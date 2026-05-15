"""Windows system wake-lock so the Chromecast proxy keeps serving while
the laptop screen is off.

The Chromecast decodes video on its own — the laptop's only job during
playback is proxying segments from the CDN with captured session cookies.
If Windows suspends our process (idle sleep / Modern Standby), the proxy
stops answering, the TV buffer drains, and playback halts. Preventing
*system* sleep (while leaving *display* sleep alone) is enough to fix it.

We hold TWO parallel keep-alive signals while a cast is active:

1. `SetThreadExecutionState(ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)` —
   the classic Win32 power-request API. Prevents legacy S3 sleep and is
   honoured on systems that still support it. Largely a no-op on systems
   with Modern Standby ("S0 Low Power Idle"), which is what every recent
   Windows 11 laptop ships with.

2. `PowerSetRequest(PowerRequestSystemRequired)` plus
   `PowerSetRequest(PowerRequestExecutionRequired)` — the modern API
   (kernel32 since Win7). PowerRequestExecutionRequired is the KEY flag
   for Modern Standby: without it the OS will throttle background
   processes to near-zero CPU once the display turns off (display-off
   triggers Connected Standby on these machines), our keepalive task
   stops firing, the WiFi radio dissociates from the AP, and the
   Chromecast errors out fetching the next segment. With this flag the
   process keeps running normally across Connected Standby entry.

All Windows API calls are funneled through a single daemon "keeper"
thread. SetThreadExecutionState is thread-scoped — the state goes away
when the thread that set it exits — and aiohttp's executor threads are
short-lived, so naive "call from wherever" would drop the lock as soon
as play() returns. The keeper thread lives for the life of the process.

On non-Windows platforms every public function is a no-op so the import
stays safe in tests / future ports.
"""

from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
import logging
import platform
import queue
import threading
from typing import Optional

log = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

# --- SetThreadExecutionState constants (legacy path) ---
# https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040

# Flag combo: keep the system awake (incl. legacy AWAYMODE), but do NOT set
# ES_DISPLAY_REQUIRED — screen-off must still work.
_HOLD_FLAGS = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
_CLEAR_FLAGS = ES_CONTINUOUS  # ES_CONTINUOUS alone reverts to normal.

# --- PowerCreateRequest constants (Modern Standby path) ---
# https://learn.microsoft.com/en-us/windows/win32/api/winnt/ne-winnt-power_request_type
_PowerRequestSystemRequired = 1
_PowerRequestExecutionRequired = 3

# REASON_CONTEXT.Version / .Flags
_POWER_REQUEST_CONTEXT_VERSION = 0
_POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x00000001

# PowerSet/Clear request types we toggle when the wakelock transitions to /
# from "held". SystemRequired is the broad "don't sleep" signal;
# ExecutionRequired is what specifically keeps user-mode processes runnable
# while the OS is in Connected Standby (Modern Standby).
_POWER_REQUEST_TYPES = (
    _PowerRequestSystemRequired,
    _PowerRequestExecutionRequired,
)


class _ReasonContext(ctypes.Structure):
    """REASON_CONTEXT with the SimpleReasonString variant.

    The real struct's third field is a union; when Flags ==
    POWER_REQUEST_CONTEXT_SIMPLE_STRING, the OS reads it as an LPWSTR
    starting right after Flags. Modelling only that variant is fine
    because the API only touches the union member that Flags selects.
    """

    _fields_ = [
        ("Version", wintypes.ULONG),
        ("Flags", wintypes.DWORD),
        ("SimpleReasonString", wintypes.LPWSTR),
    ]


_lock = threading.Lock()
_count = 0
_keeper_thread: Optional[threading.Thread] = None
_cmd_queue: "queue.Queue[str]" = queue.Queue()
_atexit_registered = False
# PowerCreateRequest handle, cached for the life of the process and reused
# across every hold/clear cycle. Created lazily on first hold. None means
# either non-Windows, the create call failed, or we haven't tried yet.
_power_handle: Optional[int] = None
_power_create_attempted = False


def _call_stes(flags: int, reason: str) -> None:
    if not _IS_WINDOWS:
        return
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # Pass a plain int — ctypes handles DWORD conversion. Setting
        # argtypes/restype here would break unit-test mocks without
        # materially changing runtime behavior.
        prev = kernel32.SetThreadExecutionState(flags)
        # Windows returns a DWORD; Python sees it as a signed int which
        # makes %X print "0x-80000000". Mask for a clean unsigned log.
        try:
            prev = int(prev) & 0xFFFFFFFF
        except (TypeError, ValueError):
            pass
        if prev == 0:
            log.warning(
                "SetThreadExecutionState(0x%X) returned 0 — power request may"
                " be ignored by policy (reason: %s)",
                flags,
                reason,
            )
        else:
            log.info(
                "SetThreadExecutionState(0x%X) ok, prev=0x%X (reason: %s)",
                flags,
                prev,
                reason,
            )
    except OSError:
        log.exception("SetThreadExecutionState raised (flags=0x%X)", flags)


def _ensure_power_handle() -> Optional[int]:
    """Create the PowerCreateRequest handle on first use. Cached for the
    life of the process and reused across every hold/clear cycle. Returns
    None if creation fails — callers fall back to STES alone.
    """
    global _power_handle, _power_create_attempted
    if not _IS_WINDOWS:
        return None
    if _power_handle is not None:
        return _power_handle
    if _power_create_attempted:
        # Already tried and failed; don't keep retrying on every hold.
        return None
    _power_create_attempted = True
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.PowerCreateRequest.restype = wintypes.HANDLE
        kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(_ReasonContext)]
        ctx = _ReasonContext()
        ctx.Version = _POWER_REQUEST_CONTEXT_VERSION
        ctx.Flags = _POWER_REQUEST_CONTEXT_SIMPLE_STRING
        ctx.SimpleReasonString = "Cast Booster — active Chromecast session"
        h = kernel32.PowerCreateRequest(ctypes.byref(ctx))
    except (OSError, AttributeError, TypeError):
        log.exception("PowerCreateRequest raised")
        return None
    if not h:
        log.warning(
            "PowerCreateRequest returned NULL — Modern Standby protection"
            " unavailable; falling back to STES only"
        )
        return None
    try:
        _power_handle = int(h)
    except (TypeError, ValueError):
        log.exception("PowerCreateRequest returned non-handle: %r", h)
        _power_handle = None
        return None
    log.info("PowerCreateRequest ok (handle=0x%X)", _power_handle)
    return _power_handle


def _call_power_set(reason: str) -> None:
    """Tell the OS we need to stay runnable (system + execution required).
    No-op if PowerCreateRequest never succeeded — caller still gets STES.
    """
    if not _IS_WINDOWS:
        return
    h = _ensure_power_handle()
    if h is None:
        return
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.PowerSetRequest.restype = wintypes.BOOL
        kernel32.PowerSetRequest.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    except (OSError, AttributeError):
        log.exception("PowerSetRequest setup raised")
        return
    for req_type in _POWER_REQUEST_TYPES:
        try:
            ok = kernel32.PowerSetRequest(h, req_type)
        except OSError:
            log.exception("PowerSetRequest(type=%d) raised", req_type)
            continue
        if not ok:
            log.warning(
                "PowerSetRequest(type=%d) returned 0 (reason: %s)",
                req_type,
                reason,
            )
        else:
            log.info(
                "PowerSetRequest(type=%d) ok (reason: %s)", req_type, reason
            )


def _call_power_clear(reason: str) -> None:
    if not _IS_WINDOWS:
        return
    h = _ensure_power_handle()
    if h is None:
        return
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.PowerClearRequest.restype = wintypes.BOOL
        kernel32.PowerClearRequest.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    except (OSError, AttributeError):
        log.exception("PowerClearRequest setup raised")
        return
    for req_type in _POWER_REQUEST_TYPES:
        try:
            ok = kernel32.PowerClearRequest(h, req_type)
        except OSError:
            log.exception("PowerClearRequest(type=%d) raised", req_type)
            continue
        if not ok:
            # ERROR_NOT_FOUND is benign: that request type wasn't set.
            log.debug(
                "PowerClearRequest(type=%d) returned 0 (reason: %s)",
                req_type,
                reason,
            )
        else:
            log.info(
                "PowerClearRequest(type=%d) ok (reason: %s)", req_type, reason
            )


def _keeper_loop() -> None:
    while True:
        cmd = _cmd_queue.get()
        if cmd == "hold":
            _call_stes(_HOLD_FLAGS, "cast session active")
            _call_power_set("cast session active")
        elif cmd == "clear":
            _call_power_clear("no cast sessions")
            _call_stes(_CLEAR_FLAGS, "no cast sessions")
        elif cmd == "exit":
            _call_power_clear("keeper thread exiting")
            _call_stes(_CLEAR_FLAGS, "keeper thread exiting")
            return


def _ensure_keeper() -> None:
    global _keeper_thread, _atexit_registered
    if _keeper_thread is None or not _keeper_thread.is_alive():
        _keeper_thread = threading.Thread(
            target=_keeper_loop, name="wakelock-keeper", daemon=True
        )
        _keeper_thread.start()
    if not _atexit_registered:
        atexit.register(force_release_all)
        _atexit_registered = True


def acquire(reason: str) -> None:
    """Increment the wake-lock ref count. Prevents system sleep on the
    first call; later calls just bump the counter. Safe from any thread."""
    global _count
    if not _IS_WINDOWS:
        return
    with _lock:
        _ensure_keeper()
        _count += 1
        new_count = _count
        if new_count == 1:
            _cmd_queue.put("hold")
    log.debug("wakelock.acquire -> count=%d (%s)", new_count, reason)


def release() -> None:
    """Decrement the wake-lock ref count. On transition to 0, lets the
    system sleep again. Safe from any thread. Never goes negative."""
    global _count
    if not _IS_WINDOWS:
        return
    with _lock:
        if _count <= 0:
            log.debug("wakelock.release called with count already 0 — ignoring")
            return
        _count -= 1
        new_count = _count
        if new_count == 0:
            _cmd_queue.put("clear")
    log.debug("wakelock.release -> count=%d", new_count)


def force_release_all() -> None:
    """Zero the counter and clear the power request. Idempotent; safe to
    call from atexit / signal handlers / shutdown paths."""
    global _count
    if not _IS_WINDOWS:
        return
    with _lock:
        was_held = _count > 0
        _count = 0
    if was_held:
        _cmd_queue.put("clear")
        log.info("wakelock.force_release_all — cleared active hold")


def is_held() -> bool:
    with _lock:
        return _count > 0
