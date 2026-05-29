"""Chrome Native Messaging stdio host.

Chrome spawns this process once per `chrome.runtime.connectNative(...)` call.
The process reads length-prefixed JSON frames from stdin, forwards each to the
long-running Cast Booster app over `POST http://127.0.0.1:38123/nm`, and writes
the response back as another framed JSON message on stdout.

If the app isn't running when we try to forward, we launch it detached and
wait up to 5s for /health to come up. Chrome keeps this process alive until
it decides to disconnect (usually when the page that called connectNative
unloads).
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

APP_HOST = "127.0.0.1"
APP_PORT = 38123
HEALTH_PATH = "/health"
NM_PATH = "/nm"
LAUNCH_WAIT_SECONDS = 5.0
# 2026-05-17: the prior 0.8s timeout fired during transient asyncio-loop
# freezes (notably pychromecast SSL reconnect, which stalls the proxy for a
# few seconds on Python 3.14) and caused nm_host to spawn duplicate detached
# processes — observed 62 times in a single session. Give the app enough
# headroom to recover before declaring it dead.
HEALTH_TIMEOUT_SECONDS = 2.5
BUSY_RETRY_DELAY_SECONDS = 0.5

log = logging.getLogger("castbooster.nm_host")


def _configure_log() -> None:
    # Separate log so stdio isn't polluted and to aid debugging NM startup failures.
    try:
        from castbooster.log import _log_dir  # type: ignore

        path = _log_dir() / "nm_host.log"
    except Exception:
        path = Path.home() / "castbooster_nm_host.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _set_binary_stdio() -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)


def _read_frame() -> Optional[dict]:
    header = sys.stdin.buffer.read(4)
    if not header or len(header) < 4:
        return None
    (length,) = struct.unpack("<I", header)
    if length == 0:
        return None
    body = sys.stdin.buffer.read(length)
    if len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


def _write_frame(obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _app_health_ok() -> bool:
    try:
        conn = http.client.HTTPConnection(APP_HOST, APP_PORT, timeout=0.8)
        try:
            conn.request("GET", HEALTH_PATH)
            resp = conn.getresponse()
            return resp.status == 200
        finally:
            conn.close()
    except Exception:
        return False


def _app_status() -> str:
    """Probe /health on the running app.

    Returns one of:
      "ok"   — /health responded 200
      "dead" — TCP connection refused (port not bound; app is genuinely gone)
      "busy" — port is bound but the request timed out or returned non-200
               (likely a transient asyncio-loop freeze, e.g. pychromecast SSL
               reconnect on Python 3.14). Caller should retry once before
               assuming the app is dead.
    """
    try:
        conn = http.client.HTTPConnection(APP_HOST, APP_PORT, timeout=HEALTH_TIMEOUT_SECONDS)
        try:
            conn.request("GET", HEALTH_PATH)
            resp = conn.getresponse()
            return "ok" if resp.status == 200 else "busy"
        finally:
            conn.close()
    except ConnectionRefusedError:
        return "dead"
    except Exception:
        return "busy"


def _launch_app_detached() -> None:
    """Spawn the main Cast Booster tray app as a detached background process.

    Uses the same Python interpreter that launched us (the venv's python.exe)
    so PATH/site-packages resolution matches.
    """
    python = sys.executable
    cmd = [python, "-m", "castbooster"]

    creationflags = 0
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

    log.info("launching app detached: %s", cmd)
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


def _ensure_app_running() -> bool:
    status = _app_status()
    if status == "ok":
        return True
    if status == "busy":
        # Port is bound but response was slow / non-200 — likely a transient
        # asyncio-loop freeze. Wait briefly and retry once before spawning a
        # duplicate. Duplicates cost us session-state loss + a 404 cascade on
        # the Chromecast (the new process doesn't know the old session token).
        time.sleep(BUSY_RETRY_DELAY_SECONDS)
        if _app_status() == "ok":
            return True
    _launch_app_detached()
    deadline = time.monotonic() + LAUNCH_WAIT_SECONDS
    while time.monotonic() < deadline:
        if _app_health_ok():
            return True
        time.sleep(0.25)
    return False


# P3.6: cloud cast needs ample boot budget — cold image pull on a fresh
# pod can take 3-6 min, plus another 60-90s for TRT engine compile + first
# segments. The orchestrator's healthz_boot_timeout_s is 360s and
# playlist_ready_timeout_s is 90s, so total worst case is ~7.5 min. Add
# 30s safety margin → 480s here. The popup card UI will show progress
# during the wait so the user isn't staring at a frozen extension.
# Pre-cloud value was 30s (sufficient for pure local-proxy operations).
_NM_FORWARD_TIMEOUT_S = 480


def _forward_to_app(payload: dict) -> dict:
    conn = http.client.HTTPConnection(APP_HOST, APP_PORT, timeout=_NM_FORWARD_TIMEOUT_S)
    try:
        body = json.dumps(payload).encode("utf-8")
        conn.request(
            "POST",
            NM_PATH,
            body=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        resp = conn.getresponse()
        data = resp.read()
        try:
            return json.loads(data.decode("utf-8"))
        except Exception as e:
            return {
                "type": "error",
                "detail": f"malformed app response: {e}",
            }
    finally:
        conn.close()


def main() -> int:
    _configure_log()
    _set_binary_stdio()
    log.info("nm_host started, python=%s", sys.executable)
    try:
        while True:
            msg = _read_frame()
            if msg is None:
                log.info("stdin closed, exiting")
                return 0
            log.info("recv: type=%s", msg.get("type"))
            if not _ensure_app_running():
                _write_frame(
                    {
                        "type": "error",
                        "detail": "Cast Booster app is not running and auto-launch failed. Check the log.",
                    }
                )
                continue
            try:
                resp = _forward_to_app(msg)
            except Exception as e:
                log.exception("forward failed")
                resp = {"type": "error", "detail": f"forward failed: {e}"}
            _write_frame(resp)
    except Exception:
        log.exception("nm_host crashed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
