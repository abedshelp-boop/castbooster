"""Tests for the Windows wake-lock module.

The module is a no-op off Windows, so we patch `_IS_WINDOWS` + the
`ctypes.windll` attribute to run the real logic in any environment.
SetThreadExecutionState is stubbed to a recording Mock; we assert the
ref-count transitions drive the expected Windows API flag sequences.

The keeper thread runs asynchronously — we drain commands by polling
the mock's call count with a small timeout instead of sleeping blindly.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from castbooster import wakelock


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040
HOLD = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
CLEAR = ES_CONTINUOUS


def _wait_for_calls(mock: MagicMock, at_least: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while mock.call_count < at_least:
        if time.monotonic() > deadline:
            raise AssertionError(
                f"expected >= {at_least} STES calls, got {mock.call_count}"
            )
        time.sleep(0.01)


@pytest.fixture
def stes_mock(monkeypatch):
    """Patch ctypes.windll.kernel32.SetThreadExecutionState and force the
    module to think it's on Windows. Reset the module's global state so
    tests don't bleed into each other.

    Also patches PowerCreateRequest / PowerSetRequest / PowerClearRequest so
    the Modern-Standby path runs through alongside STES. The fixture returns
    the STES mock for legacy assertions; for tests that need to inspect the
    PowerRequest calls, use the `power_mocks` fixture below.
    """
    # Force-Windows path.
    monkeypatch.setattr(wakelock, "_IS_WINDOWS", True)

    # Stub the ctypes surface the module touches. PowerCreateRequest must
    # return a non-zero int so _ensure_power_handle caches a real handle
    # and the Power{Set,Clear}Request mocks actually run.
    fake_stes = MagicMock(return_value=0x1)
    fake_create = MagicMock(return_value=0xDEADBEEF)
    fake_set = MagicMock(return_value=1)
    fake_clear = MagicMock(return_value=1)
    fake_kernel32 = MagicMock()
    fake_kernel32.SetThreadExecutionState = fake_stes
    fake_kernel32.PowerCreateRequest = fake_create
    fake_kernel32.PowerSetRequest = fake_set
    fake_kernel32.PowerClearRequest = fake_clear
    fake_windll = MagicMock()
    fake_windll.kernel32 = fake_kernel32
    # ctypes.windll only exists on Windows; set it so _call_stes can reach it.
    import ctypes as _ctypes

    monkeypatch.setattr(_ctypes, "windll", fake_windll, raising=False)

    # Reset module globals so each test starts clean.
    wakelock._count = 0
    wakelock._power_handle = None
    wakelock._power_create_attempted = False
    # Drain any leftover queue items from prior tests without blocking.
    try:
        while True:
            wakelock._cmd_queue.get_nowait()
    except Exception:
        pass
    # Kill any prior keeper thread by sending it an exit signal, then
    # let _ensure_keeper spin up a fresh one on the next acquire.
    if wakelock._keeper_thread is not None and wakelock._keeper_thread.is_alive():
        wakelock._cmd_queue.put("exit")
        wakelock._keeper_thread.join(timeout=1.0)
    wakelock._keeper_thread = None
    wakelock._atexit_registered = False  # avoid stacking atexit registrations

    # Expose the power mocks as attributes on the STES mock for tests that
    # want to inspect them, while keeping the existing API (legacy tests
    # only need fake_stes) unchanged.
    fake_stes.power_create = fake_create
    fake_stes.power_set = fake_set
    fake_stes.power_clear = fake_clear

    yield fake_stes

    # Teardown: drop any remaining hold.
    wakelock.force_release_all()
    if wakelock._keeper_thread is not None and wakelock._keeper_thread.is_alive():
        wakelock._cmd_queue.put("exit")
        wakelock._keeper_thread.join(timeout=1.0)


def _flag_calls(mock: MagicMock):
    return [int(c.args[0]) for c in mock.call_args_list]


def test_acquire_calls_hold_with_system_and_awaymode(stes_mock):
    wakelock.acquire("test")
    _wait_for_calls(stes_mock, 1)
    assert stes_mock.call_args_list[0].args[0] == HOLD
    assert wakelock.is_held()


def test_double_acquire_single_api_call(stes_mock):
    wakelock.acquire("a")
    wakelock.acquire("b")
    _wait_for_calls(stes_mock, 1)
    # Give the keeper a beat to (not) receive a second command.
    time.sleep(0.05)
    assert stes_mock.call_count == 1
    assert wakelock.is_held()


def test_release_matches_acquires_and_clears_on_zero(stes_mock):
    wakelock.acquire("a")
    wakelock.acquire("b")
    wakelock.release()  # count: 2 -> 1, no API call
    _wait_for_calls(stes_mock, 1)
    time.sleep(0.05)
    assert stes_mock.call_count == 1, "release at count>0 must not call API"
    assert wakelock.is_held()

    wakelock.release()  # count: 1 -> 0, should clear
    _wait_for_calls(stes_mock, 2)
    assert _flag_calls(stes_mock) == [HOLD, CLEAR]
    assert not wakelock.is_held()


def test_release_when_not_held_is_noop(stes_mock):
    wakelock.release()
    time.sleep(0.05)
    assert stes_mock.call_count == 0
    assert not wakelock.is_held()


def test_force_release_all_from_nonzero_count(stes_mock):
    wakelock.acquire("a")
    wakelock.acquire("b")
    wakelock.acquire("c")
    _wait_for_calls(stes_mock, 1)
    assert wakelock.is_held()

    wakelock.force_release_all()
    _wait_for_calls(stes_mock, 2)
    assert _flag_calls(stes_mock)[-1] == CLEAR
    assert not wakelock.is_held()


def test_force_release_all_when_idle_is_noop(stes_mock):
    wakelock.force_release_all()
    time.sleep(0.05)
    assert stes_mock.call_count == 0


def test_reacquire_after_release(stes_mock):
    wakelock.acquire("a")
    _wait_for_calls(stes_mock, 1)
    wakelock.release()
    _wait_for_calls(stes_mock, 2)
    wakelock.acquire("b")
    _wait_for_calls(stes_mock, 3)
    assert _flag_calls(stes_mock) == [HOLD, CLEAR, HOLD]
    assert wakelock.is_held()


def test_non_windows_is_noop(monkeypatch):
    """On non-Windows, public API must not touch ctypes at all."""
    monkeypatch.setattr(wakelock, "_IS_WINDOWS", False)
    # Reset count to exercise the early-return branches.
    wakelock._count = 0

    # If _call_stes were invoked, this would blow up because windll may
    # not exist on non-Windows platforms. The test passes by not raising.
    wakelock.acquire("x")
    wakelock.release()
    wakelock.force_release_all()
    assert not wakelock.is_held()


# ---------------------------------------------------------------------------
# Modern-Standby (PowerCreateRequest / PowerSetRequest / PowerClearRequest)
# ---------------------------------------------------------------------------


def test_hold_calls_power_set_with_system_and_execution(stes_mock):
    """When the wakelock transitions to held, PowerSetRequest must be
    called with PowerRequestSystemRequired (1) AND PowerRequestExecutionRequired
    (3). The latter is the key flag — without it, the OS will throttle the
    proxy during Connected Standby and casting stalls when the screen turns off.
    """
    wakelock.acquire("test")
    _wait_for_calls(stes_mock, 1)
    # Give the keeper a beat for the follow-up PowerSetRequest calls.
    deadline = time.monotonic() + 2.0
    while stes_mock.power_set.call_count < 2:
        if time.monotonic() > deadline:
            raise AssertionError(
                f"expected >= 2 PowerSetRequest calls, got "
                f"{stes_mock.power_set.call_count}"
            )
        time.sleep(0.01)
    req_types = sorted(c.args[1] for c in stes_mock.power_set.call_args_list)
    assert req_types == [1, 3], (
        f"expected SystemRequired (1) and ExecutionRequired (3), got {req_types}"
    )


def test_clear_calls_power_clear_with_same_types(stes_mock):
    """Releasing the last ref must clear both request types we set."""
    wakelock.acquire("a")
    _wait_for_calls(stes_mock, 1)
    deadline = time.monotonic() + 2.0
    while stes_mock.power_set.call_count < 2:
        if time.monotonic() > deadline:
            raise AssertionError("PowerSetRequest never fired on hold")
        time.sleep(0.01)
    wakelock.release()
    _wait_for_calls(stes_mock, 2)
    deadline = time.monotonic() + 2.0
    while stes_mock.power_clear.call_count < 2:
        if time.monotonic() > deadline:
            raise AssertionError("PowerClearRequest never fired on clear")
        time.sleep(0.01)
    req_types = sorted(c.args[1] for c in stes_mock.power_clear.call_args_list)
    assert req_types == [1, 3]


def test_power_create_handle_is_cached_across_cycles(stes_mock):
    """PowerCreateRequest is expensive-ish; we should create the handle
    once and reuse it across hold/clear cycles for the life of the process."""
    wakelock.acquire("a")
    _wait_for_calls(stes_mock, 1)
    deadline = time.monotonic() + 2.0
    while stes_mock.power_set.call_count < 2:
        if time.monotonic() > deadline:
            break
        time.sleep(0.01)
    wakelock.release()
    _wait_for_calls(stes_mock, 2)
    wakelock.acquire("b")
    _wait_for_calls(stes_mock, 3)
    deadline = time.monotonic() + 2.0
    while stes_mock.power_set.call_count < 4:
        if time.monotonic() > deadline:
            break
        time.sleep(0.01)
    # Two full hold/clear/hold cycles, one create call total.
    assert stes_mock.power_create.call_count == 1


def test_power_create_failure_falls_back_to_stes_only(monkeypatch, stes_mock):
    """If PowerCreateRequest returns NULL (e.g. older Windows or policy
    blocked), the wakelock must still hold via STES alone — don't crash
    and don't keep retrying the failing create call on every hold."""
    # Make PowerCreateRequest return 0 (NULL handle).
    stes_mock.power_create.return_value = 0
    wakelock._power_handle = None
    wakelock._power_create_attempted = False

    wakelock.acquire("a")
    _wait_for_calls(stes_mock, 1)
    # Give the keeper a beat to NOT call PowerSetRequest (handle is None).
    time.sleep(0.1)
    assert stes_mock.power_set.call_count == 0
    # STES path still fires.
    assert _flag_calls(stes_mock)[0] == HOLD

    # Re-acquire after release — should not retry PowerCreateRequest.
    wakelock.release()
    _wait_for_calls(stes_mock, 2)
    wakelock.acquire("b")
    _wait_for_calls(stes_mock, 3)
    time.sleep(0.1)
    assert stes_mock.power_create.call_count == 1, (
        "PowerCreateRequest must not be retried after the first failure"
    )
