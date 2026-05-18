"""Manual end-to-end acceptance harness for the Pillar 2 cast pipeline.

Run:
    cd app
    .venv\\Scripts\\python -m castbooster.acceptance --cast-uuid <uuid>            # smoke (60s)
    .venv\\Scripts\\python -m castbooster.acceptance --mode soak --minutes 30 ...  # soak (DoD gate)

Requires a real Chromecast on the same LAN. The tray app MUST NOT be
running on port 38123 — this script starts its own proxy in-process.

Used to verify the Pillar 2 ship gate; the 30-minute soak run produces
the result for roadmap §7 DoD.

Exit codes: 0=all PASS, 1=any FAIL, 2=bad CLI, 130=Ctrl+C.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from castbooster import ffmpeg_probe
from castbooster import proxy as proxy_mod
from castbooster.ffmpeg_probe import FFmpegNotFoundError, FFmpegProbeError
from castbooster.proxy import DEFAULT_UA, PROXY_PORT

log = logging.getLogger("castbooster.acceptance")

_NM_URL = f"http://127.0.0.1:{PROXY_PORT}/nm"
# Apple's public bipbop test stream — short, reliable, plain HLS.
_DEFAULT_STREAM = (
    "https://devstreaming-cdn.apple.com/videos/streaming/examples/"
    "bipbop_4x3/bipbop_4x3_variant.m3u8"
)
_TERMINAL_IDLE = {"FINISHED", "CANCELLED", "ERROR", "INTERRUPTED"}
_DISCOVERY_WAIT_S = 5.0
_POLL_INTERVAL_S = 2.0
_SMOKE_WINDOW_S = 60.0
_STOP_DRAIN_S = 3.0


@dataclass
class StepResult:
    name: str
    passed: bool
    detail: str
    elapsed: float


@dataclass
class HarnessState:
    """Carries values produced by earlier steps into later ones."""
    proxy_handle: Optional[proxy_mod.ProxyHandle] = None
    token: str = ""
    matched_cast_name: str = ""


def _nm_post(payload: dict, timeout: float = 30.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _NM_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _run_step(name: str, fn: Callable[[], Tuple[bool, str]]) -> StepResult:
    t0 = time.monotonic()
    try:
        passed, detail = fn()
    except Exception as e:
        log.exception("step %s raised", name)
        passed = False
        detail = f"exception: {type(e).__name__}: {e}"
    elapsed = time.monotonic() - t0
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name:<28} ({elapsed:6.2f}s)  {detail}", flush=True)
    return StepResult(name=name, passed=passed, detail=detail, elapsed=elapsed)


# ---------- steps ----------------------------------------------------------

def _step_ffmpeg_probe(_state: HarnessState) -> Tuple[bool, str]:
    try:
        accel = ffmpeg_probe.detect()
    except (FFmpegNotFoundError, FFmpegProbeError) as e:
        return False, str(e)
    return True, f"tier={accel.tier} encoder={accel.encoder} decoder={accel.decoder}"


def _step_proxy_start(state: HarnessState) -> Tuple[bool, str]:
    handle = proxy_mod.start_proxy()
    state.proxy_handle = handle
    if not handle.wait_ready(5.0):
        return False, f"wait_ready(5.0) timed out or bind failed (bind_failed={handle.bind_failed})"
    return True, f"lan_ip={handle.lan_ip} port={PROXY_PORT}"


def _step_cast_discovery(state: HarnessState, cast_uuid: str) -> Tuple[bool, str]:
    # zeroconf needs a few seconds to populate the device list after the
    # proxy's CastManager fires up.
    time.sleep(_DISCOVERY_WAIT_S)
    resp = _nm_post({"type": "list_casts"})
    casts = resp.get("casts", []) if isinstance(resp, dict) else []
    matched = next((c for c in casts if c.get("uuid") == cast_uuid), None)
    if matched is None:
        seen = [(c.get("name"), (c.get("uuid") or "")[:8]) for c in casts]
        return False, f"target uuid {cast_uuid} not found; discovered={seen}"
    state.matched_cast_name = matched.get("name", "")
    return True, f"{matched.get('name')} ({matched.get('model')})"


def _step_register_stream(state: HarnessState, url: str) -> Tuple[bool, str]:
    resp = _nm_post({
        "type": "register_stream",
        "url": url,
        "cookies": [],
        "headers": {},
        "userAgent": DEFAULT_UA,
    })
    if not isinstance(resp, dict) or resp.get("type") != "stream_registered":
        return False, f"unexpected response: {resp}"
    token = resp.get("token")
    content_type = resp.get("contentType", "")
    if not token or not isinstance(token, str):
        return False, f"missing token in response: {resp}"
    state.token = token
    return True, f"token={token[:8]} ct={content_type}"


def _step_cast(state: HarnessState, cast_uuid: str) -> Tuple[bool, str]:
    # `cast` blocks server-side until transcoder is READY (per-tier warming
    # budget), then play_media is invoked. Generous client-side timeout.
    resp = _nm_post({
        "type": "cast",
        "token": state.token,
        "castUuid": cast_uuid,
    }, timeout=60.0)
    if not isinstance(resp, dict) or resp.get("type") != "casting":
        return False, f"unexpected response: {resp}"
    if resp.get("status") != "ok":
        return False, f"status={resp.get('status')} detail={resp.get('detail')}"
    playback_url = resp.get("playbackUrl", "")
    if "/output/master.m3u8" not in playback_url:
        # passthrough fallback means the transcoder failed to come up —
        # acceptance gate requires the transcoder path
        return False, f"playback fell back to /upstream/ (passthrough): {playback_url}"
    return True, f"playbackUrl={playback_url}"


def _step_sustain(
    state: HarnessState, cast_uuid: str, window_s: float,
) -> Tuple[bool, str]:
    deadline = time.monotonic() + window_s
    iterations = 0
    seen_playing = False
    max_current_time = 0.0
    terminal_idle_seen: Optional[str] = None

    while time.monotonic() < deadline:
        iterations += 1
        try:
            resp = _nm_post({"type": "media_status", "castUuid": cast_uuid})
        except Exception as e:
            log.warning("media_status poll #%d failed: %s", iterations, e)
            time.sleep(_POLL_INTERVAL_S)
            continue
        state_str = resp.get("state", "") if isinstance(resp, dict) else ""
        idle_reason = resp.get("idleReason", "") if isinstance(resp, dict) else ""
        current_time = 0.0
        if isinstance(resp, dict):
            ct_raw = resp.get("currentTime", 0)
            try:
                current_time = float(ct_raw or 0)
            except (TypeError, ValueError):
                current_time = 0.0
        if state_str in ("PLAYING", "BUFFERING"):
            seen_playing = True
        if current_time > max_current_time:
            max_current_time = current_time
        # Only count terminal IDLE after we've already seen the session reach
        # an active state — there's a brief IDLE window right after cast
        # before play_media latches on the device.
        if (
            state_str == "IDLE"
            and idle_reason in _TERMINAL_IDLE
            and seen_playing
        ):
            terminal_idle_seen = idle_reason
            break
        log.debug(
            "poll #%d state=%s reason=%s ct=%.1f",
            iterations, state_str, idle_reason, current_time,
        )
        time.sleep(_POLL_INTERVAL_S)

    elapsed = window_s - max(0.0, deadline - time.monotonic())
    if not seen_playing:
        return False, f"never reached PLAYING state ({iterations} polls)"
    if terminal_idle_seen is not None:
        return False, f"terminal IDLE: {terminal_idle_seen} (after {iterations} polls)"
    if max_current_time <= 0:
        return False, f"currentTime never advanced past 0 ({iterations} polls)"
    return True, (
        f"polled {iterations}x over {elapsed:.0f}s, "
        f"max_currentTime={max_current_time:.1f}s, never IDLE"
    )


def _step_clean_stop(state: HarnessState, cast_uuid: str) -> Tuple[bool, str]:
    resp = _nm_post({
        "type": "media_cmd",
        "castUuid": cast_uuid,
        "action": "stop",
    })
    if not isinstance(resp, dict) or resp.get("type") != "media_cmd_result":
        return False, f"unexpected response: {resp}"
    if resp.get("status") != "ok":
        return False, f"status={resp.get('status')} detail={resp.get('detail')}"
    # Give Transcoder.stop() time to drain the slot.
    time.sleep(_STOP_DRAIN_S)
    return True, "stop ok"


# ---------- discovery helper for missing-uuid error ------------------------

def _print_lan_casts_then_exit() -> None:
    """Spin up the proxy briefly, list discovered Chromecasts, then exit 2."""
    print("error: --cast-uuid is required (or env CASTBOOSTER_CAST_UUID).", file=sys.stderr)
    print("Attempting LAN discovery to help...", file=sys.stderr)
    handle: Optional[proxy_mod.ProxyHandle] = None
    try:
        handle = proxy_mod.start_proxy()
        if handle.wait_ready(5.0):
            time.sleep(_DISCOVERY_WAIT_S)
            try:
                resp = _nm_post({"type": "list_casts"})
                casts = resp.get("casts", []) if isinstance(resp, dict) else []
                if casts:
                    print("Discovered Chromecasts on this LAN:", file=sys.stderr)
                    for c in casts:
                        print(
                            f"  - {c.get('name')!r:30s} uuid={c.get('uuid')} model={c.get('model')!r}",
                            file=sys.stderr,
                        )
                else:
                    print("(none found — make sure the device is on and same Wi-Fi)", file=sys.stderr)
            except Exception as e:
                print(f"  (discovery probe failed: {e})", file=sys.stderr)
        else:
            print("(proxy failed to bind — is the tray app already running on 38123?)", file=sys.stderr)
    finally:
        if handle is not None:
            try:
                handle.stop()
            except Exception:
                pass
    sys.exit(2)


# ---------- orchestrator + CLI ---------------------------------------------

def _summarize(results: List[StepResult], mode: str) -> int:
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    total_elapsed = sum(r.elapsed for r in results)
    print("=" * 40, flush=True)
    print("========== ACCEPTANCE SUMMARY ==========", flush=True)
    for r in results:
        tag = "PASS" if r.passed else "FAIL"
        print(f"[{tag}] {r.name:<28} ({r.elapsed:6.2f}s)  {r.detail}", flush=True)
    print("=" * 40, flush=True)
    print(f"Total: {passed}/{total} PASS, elapsed={total_elapsed:.1f}s", flush=True)
    print(f"Mode: {mode}", flush=True)
    if passed != total:
        first_fail = next(r for r in results if not r.passed)
        print(f"First failure: {first_fail.name} — {first_fail.detail}", flush=True)
        return 1
    return 0


def run(args: argparse.Namespace) -> int:
    state = HarnessState()
    results: List[StepResult] = []
    cast_uuid: str = args.cast_uuid
    url: str = args.url
    window_s = _SMOKE_WINDOW_S if args.mode == "smoke" else float(args.minutes) * 60.0

    def _abort_remaining() -> int:
        return _summarize(results, args.mode)

    try:
        results.append(_run_step("step-1-ffmpeg-probe", lambda: _step_ffmpeg_probe(state)))
        if not results[-1].passed:
            return _abort_remaining()

        results.append(_run_step("step-2-proxy-start", lambda: _step_proxy_start(state)))
        if not results[-1].passed:
            return _abort_remaining()

        results.append(_run_step(
            "step-3-cast-discovery",
            lambda: _step_cast_discovery(state, cast_uuid),
        ))
        if not results[-1].passed:
            return _abort_remaining()

        results.append(_run_step(
            "step-4-register-stream",
            lambda: _step_register_stream(state, url),
        ))
        if not results[-1].passed:
            return _abort_remaining()

        results.append(_run_step("step-5-cast", lambda: _step_cast(state, cast_uuid)))
        if not results[-1].passed:
            return _abort_remaining()

        results.append(_run_step(
            f"step-6-sustain-{int(window_s)}s",
            lambda: _step_sustain(state, cast_uuid, window_s),
        ))
        sustain_passed = results[-1].passed

        # Always attempt clean stop even if sustain failed, so the device
        # doesn't get left spinning on a half-dead session.
        results.append(_run_step(
            "step-7-clean-stop",
            lambda: _step_clean_stop(state, cast_uuid),
        ))

        return _summarize(results, args.mode)
    finally:
        if state.proxy_handle is not None:
            try:
                state.proxy_handle.stop()
            except Exception:
                log.exception("proxy_handle.stop() during cleanup failed")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m castbooster.acceptance",
        description=(
            "End-to-end acceptance harness for the Pillar 2 cast pipeline. "
            "Starts its own proxy in-process — the tray app must NOT be "
            "running on port 38123 concurrently."
        ),
    )
    p.add_argument(
        "--mode", choices=("smoke", "soak"), default="smoke",
        help="smoke=60s window (default); soak=--minutes window (DoD gate)",
    )
    p.add_argument(
        "--minutes", type=int, default=30,
        help="sustain window in minutes for soak mode (default 30)",
    )
    p.add_argument(
        "--cast-uuid", default=None,
        help="target Chromecast UUID (or set env CASTBOOSTER_CAST_UUID)",
    )
    p.add_argument(
        "--url", default=None,
        help="HLS stream URL to cast (or set env CASTBOOSTER_ACCEPTANCE_URL)",
    )
    p.add_argument("--verbose", action="store_true", help="DEBUG logging")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Resolve UUID + URL from CLI → env → default.
    args.cast_uuid = args.cast_uuid or os.environ.get("CASTBOOSTER_CAST_UUID")
    args.url = args.url or os.environ.get("CASTBOOSTER_ACCEPTANCE_URL") or _DEFAULT_STREAM

    # Simple stderr logging — we're not booting the tray app's log.setup_logging().
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.INFO,
            stream=sys.stderr,
            format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        )

    if not args.cast_uuid:
        _print_lan_casts_then_exit()
        return 2  # unreachable; _print_lan_casts_then_exit calls sys.exit

    print(
        f"acceptance: mode={args.mode} "
        f"window={'60s' if args.mode == 'smoke' else f'{args.minutes}min'} "
        f"uuid={args.cast_uuid} url={args.url}",
        flush=True,
    )

    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n[INTERRUPT] aborted by user", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
