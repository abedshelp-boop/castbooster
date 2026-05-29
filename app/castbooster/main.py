import atexit
import http.client
import logging
import os
import signal
import sys
import threading
from pathlib import Path

# Pillar 3.6: load .env BEFORE any castbooster.* imports so cloud config
# (CLOUD_API_KEY, RUNPOD_API_KEY, CLOUD_WORKER_IMAGE, CLOUD_GPU_TYPES) is
# in os.environ by the time main() calls _terminate_cloud_orphans_on_startup
# and by the time proxy._handle_cast loads castbooster.cloud.cloud_cast.
# Search-path: repo root (../../.env from this file) THEN cwd.
def _load_env_dotfile() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return  # python-dotenv not installed — fall back to whatever PowerShell exported
    # this file lives at app/castbooster/main.py — repo root is two parents up.
    repo_root_env = Path(__file__).resolve().parents[2] / ".env"
    if repo_root_env.is_file():
        load_dotenv(repo_root_env)
    else:
        load_dotenv()  # fall back to cwd lookup


_load_env_dotfile()

from castbooster import __version__, wakelock
from castbooster.ffmpeg_probe import (
    detect as detect_ffmpeg,
    FFmpegNotFoundError,
    FFmpegProbeError,
)
from castbooster.log import LOG_PATH, setup_logging
from castbooster.proxy import PROXY_PORT, start_proxy
from castbooster.tray import run_tray

# Pillar 3.5 thread 1: silent crashes need to leave evidence.
# Module-level flag so the try/finally in main() can read the
# reason set by the excepthooks.
_shutdown_reason: str = "normal"


def _install_sys_excepthook() -> None:
    """Route uncaught main-thread exceptions through the logger before exit."""
    if getattr(sys.excepthook, "_castbooster_installed", False):
        return
    log = logging.getLogger("castbooster")
    original = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        global _shutdown_reason
        _shutdown_reason = "exception"
        log.error(
            "FATAL unhandled exception in main thread",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        # Still call the original so the interpreter behaves normally
        # (prints to stderr if attached, exits non-zero).
        original(exc_type, exc_value, exc_tb)

    _hook._castbooster_installed = True  # type: ignore[attr-defined]
    sys.excepthook = _hook


def _install_threading_excepthook() -> None:
    """Route uncaught non-main-thread exceptions through the logger.

    Covers the keepalive thread, transcoder side-task threads, and the
    pychromecast listener thread — any of which could be the silent killer.
    """
    if getattr(threading.excepthook, "_castbooster_installed", False):
        return
    log = logging.getLogger("castbooster")
    original = threading.excepthook

    def _hook(args):
        global _shutdown_reason
        _shutdown_reason = "exception"
        log.error(
            "FATAL unhandled exception in thread %r",
            args.thread.name if args.thread else "<unknown>",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        original(args)

    _hook._castbooster_installed = True  # type: ignore[attr-defined]
    threading.excepthook = _hook


def _another_instance_healthy() -> bool:
    """Return True if something on 127.0.0.1:PROXY_PORT answers /health 200.

    nm_host races can trigger multiple parallel launches of this module; the
    first one to bind wins and the others should quietly step aside instead
    of running a tray icon with a dead proxy thread.
    """
    try:
        conn = http.client.HTTPConnection("127.0.0.1", PROXY_PORT, timeout=0.8)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            return resp.status == 200
        finally:
            conn.close()
    except Exception:
        return False


def _terminate_cloud_orphans_on_startup() -> None:
    """Best-effort: kill any pods left running from a crashed previous session.

    Reads CLOUD_API_KEY + RUNPOD_API_KEY from env. If either is missing,
    silently no-ops (the user hasn't configured cloud yet). Any exception
    is logged and swallowed — orphan cleanup must NEVER block app startup.
    """
    import os
    log = logging.getLogger("castbooster")
    if not (os.environ.get("CLOUD_API_KEY") and os.environ.get("RUNPOD_API_KEY")):
        log.debug("cloud env not configured — skipping orphan termination")
        return
    try:
        from castbooster.cloud.cloud_cast import build_orchestrator
        orch = build_orchestrator()
        count = orch.terminate_orphans(hard_cap_s=6 * 3600)
        if count:
            log.info("terminated %d cloud orphan pod(s) on startup", count)
    except Exception:
        log.exception("cloud orphan termination skipped (continuing startup)")


def _shutdown_cloud_pods_on_exit() -> None:
    """Best-effort terminate everything in cloud state. Registered via
    atexit alongside wakelock.force_release_all. Must NEVER raise —
    Python suppresses atexit exceptions but they pollute stderr.
    """
    import os
    log = logging.getLogger("castbooster")
    if not (os.environ.get("CLOUD_API_KEY") and os.environ.get("RUNPOD_API_KEY")):
        return
    try:
        from castbooster.cloud.cloud_cast import build_orchestrator
        orch = build_orchestrator()
        count = orch.shutdown_all()
        if count:
            log.info("terminated %d cloud pod(s) on exit", count)
    except Exception:
        log.exception("cloud shutdown_all on exit failed")


def main() -> int:
    setup_logging()
    _install_sys_excepthook()
    _install_threading_excepthook()
    log = logging.getLogger("castbooster")
    log.info("starting Cast Booster v%s", __version__)
    log.info("log file: %s", LOG_PATH)

    try:
        if _another_instance_healthy():
            log.info(
                "another Cast Booster instance is already running on :%d — exiting",
                PROXY_PORT,
            )
            return 0

        # Pillar 2 — probe ffmpeg once at boot so later sub-tasks (transcoder,
        # filter chain) can read the cached AccelProfile without re-running
        # subprocess calls. Must NOT raise — Phase 1 passthrough cast still
        # works without ffmpeg.
        try:
            accel = detect_ffmpeg()
            log.info("ffmpeg ready: %s", accel)
        except FFmpegNotFoundError as e:
            log.error(
                "ffmpeg missing — Pillar 2 transcode features disabled. "
                "Run app\\scripts\\fetch_ffmpeg.ps1 to install. detail=%s", e,
            )
        except FFmpegProbeError as e:
            log.error("ffmpeg probe failed — Pillar 2 transcode disabled. detail=%s", e)

        _terminate_cloud_orphans_on_startup()

        proxy = start_proxy(on_ready=lambda ip: log.info("proxy ready on LAN IP %s", ip))
        if not proxy.wait_ready(timeout=5.0):
            log.error(
                "proxy failed to bind :%d within 5s — exiting (another instance?)",
                PROXY_PORT,
            )
            return 1

        def _shutdown() -> None:
            # Drop the wake lock first so Windows can sleep again even if the
            # proxy teardown below stalls for any reason.
            wakelock.force_release_all()
            log.info("shutting down proxy")
            proxy.stop()

        # Belt-and-suspenders cleanup paths. atexit covers normal exit;
        # signal handlers cover Ctrl+C / taskkill /T / Ctrl+Break. Each calls
        # force_release_all which is idempotent.
        atexit.register(wakelock.force_release_all)
        atexit.register(_shutdown_cloud_pods_on_exit)

        def _signal_handler(signum, _frame):  # noqa: ANN001 — signal signature
            log.info("signal %s received — releasing wakelock and exiting", signum)
            wakelock.force_release_all()
            try:
                proxy.stop()
            except Exception:
                log.exception("proxy.stop() during signal handler failed")
            # Re-raise as a normal exit so atexit handlers still run.
            sys.exit(0)

        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _signal_handler)
            except (ValueError, OSError):
                # signal.signal only works on the main thread and some signals
                # aren't settable on every platform. Non-fatal.
                log.debug("could not install handler for %s", sig_name, exc_info=True)

        try:
            run_tray(on_quit=_shutdown)
        except KeyboardInterrupt:
            log.info("interrupted")
            _shutdown()
        log.info("goodbye")
        return 0
    finally:
        log.info("castbooster shutdown reason=%s", _shutdown_reason)
        logging.shutdown()


if __name__ == "__main__":
    sys.exit(main())
