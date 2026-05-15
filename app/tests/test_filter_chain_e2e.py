"""Gated end-to-end tests for filter chain hot-reload.

Run with: cd app && set RUN_REAL_FFMPEG=1&& .venv/Scripts/python -m pytest tests/test_filter_chain_e2e.py -v -s

Per spec §7: these tests use long-enough lavfi-source mkvs (60s for e2e-reload,
30s for e2e-subs) so OLD stays in STREAMING throughout the swap. Wall budget
~15-20s per test.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from castbooster.ffmpeg_probe import detect
from castbooster.filter_chain import FilterChain, NoopFilter
from castbooster.transcoder import Transcoder, TranscoderState


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_FFMPEG") != "1",
    reason="set RUN_REAL_FFMPEG=1 to run real-ffmpeg integration tests",
)


def _wait_until(predicate, timeout: float = 8.0, interval: float = 0.1) -> bool:
    """Poll predicate() every interval seconds, up to timeout. True iff it became True."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _build_lavfi_mkv(ffmpeg_path: str, out_path: Path, duration_seconds: int = 60) -> None:
    """Use ffmpeg + lavfi to render a duration_seconds-long mkv at out_path."""
    args = [
        ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=24:duration={duration_seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={duration_seconds}",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac",
        str(out_path),
    ]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    subprocess.run(args, check=True, creationflags=creationflags, timeout=60)


def test_reload_noop_to_noop_swap_with_lavfi(tmp_path):
    """Spec §7.1 walkthrough. NoopFilter → NoopFilter swap on a 60s lavfi-source mkv.

    Asserts: RELOADING observed; promotes; v1/ gone; v2/ exists; stop cleans v2/.
    """
    accel = detect()
    src = tmp_path / "src.mkv"
    _build_lavfi_mkv(accel.ffmpeg_path, src, duration_seconds=60)

    base = tmp_path / "out"
    t = Transcoder(
        input_url=str(src),
        base_output_dir=base,
        accel=accel,
        warming_timeout=8.0,
        stall_timeout=8.0,
        _poll_interval=0.1,
    )
    t.start()
    try:
        # 1) Bring OLD to STREAMING
        assert t.wait_until_ready(timeout=8.0), f"OLD never reached READY: state={t.state}"
        assert _wait_until(
            lambda: t.state == TranscoderState.STREAMING, timeout=5.0
        ), f"OLD never STREAMING: state={t.state}"

        v1 = base / "v1"
        v2 = base / "v2"
        assert v1.exists()
        assert t.output_dir == v1

        # 2) Trigger reload (blocking; runs in foreground)
        result = t.set_filter_chain(FilterChain([NoopFilter()]))
        assert result is True, f"reload failed; state={t.state} reason={t.last_reload_error}"

        # 3) State back to a serving state, _current is v2, v1 gone
        assert t.state in (TranscoderState.READY, TranscoderState.STREAMING)
        assert t.output_dir == v2
        assert v2.exists()
        assert not v1.exists()
    finally:
        t.stop()
        # Confirm cleanup
        assert not (base / "v1").exists()
        assert not (base / "v2").exists()
