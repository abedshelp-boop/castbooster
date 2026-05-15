"""Integration test for castbooster.transcoder: runs real ffmpeg end-to-end.

Generates a 5s lavfi-sourced HLS file (testsrc video + 440 Hz sine audio),
then drives the Transcoder against it and verifies it reaches READY
within 8s. Gated on RUN_REAL_FFMPEG=1 so contributors without the vendored
binary or on different platforms still pass the suite.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from castbooster import ffmpeg_probe
from castbooster.transcoder import Transcoder, TranscoderState


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_FFMPEG") != "1",
    reason="set RUN_REAL_FFMPEG=1 to exercise real ffmpeg",
)


def _generate_lavfi_hls(ffmpeg_path: str, dest: Path) -> Path:
    """Produce a 5s 320x240 HLS source at dest/master.m3u8. Returns the path."""
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "20",
        "-c:a", "aac", "-b:a", "64k",
        "-hls_time", "1", "-hls_list_size", "0",
        "-hls_segment_filename", str(dest / "seg_%03d.ts"),
        "-f", "hls", str(dest / "master.m3u8"),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"lavfi gen failed: {result.stderr}"
    return dest / "master.m3u8"


def test_real_transcoder_reaches_ready_within_8s(tmp_path):
    ffmpeg_probe.detect.cache_clear()
    accel = ffmpeg_probe.detect()
    src_master = _generate_lavfi_hls(accel.ffmpeg_path, tmp_path / "src")
    out = tmp_path / "out"

    t = Transcoder(
        input_url=str(src_master),
        output_dir=out,
        accel=accel,
    )
    t.start()
    try:
        ok = t.wait_until_ready(timeout=8.0)
        assert ok, (
            f"transcoder failed to reach READY in 8s: state={t.state} "
            f"reason={t.idle_reason} exit_code={t.exit_code}"
        )
        assert t.state in (
            TranscoderState.READY,
            TranscoderState.STREAMING,
        )
        assert (out / "master.m3u8").exists()
        assert (out / "variant.m3u8").exists()
        assert len(list(out.glob("seg_*.ts"))) >= 2
    finally:
        t.stop()
        assert t.state == TranscoderState.TERMINATED
        assert not out.exists()
