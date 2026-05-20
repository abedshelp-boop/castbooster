import atexit
import http.client
import logging
import signal
import sys

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

    sys.excepthook = _hook


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


def main() -> int:
    setup_logging()
    _install_sys_excepthook()
    log = logging.getLogger("castbooster")
    log.info("starting Cast Booster v%s", __version__)
    log.info("log file: %s", LOG_PATH)

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


if __name__ == "__main__":
    sys.exit(main())
