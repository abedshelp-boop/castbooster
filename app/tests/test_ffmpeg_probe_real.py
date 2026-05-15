"""Integration test for ffmpeg_probe: runs the real binary.

Gated on RUN_REAL_FFMPEG=1 so CI without ffmpeg or contributors without the
vendored binary still pass the test suite. On Abed's Snapdragon dev machine
the expected outcome is tier=sw, encoder=libx264.
"""
import os

import pytest

from castbooster import ffmpeg_probe


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_FFMPEG") != "1",
    reason="set RUN_REAL_FFMPEG=1 to exercise real ffmpeg",
)


def test_real_detect_returns_a_working_profile():
    ffmpeg_probe.detect.cache_clear()
    profile = ffmpeg_probe.detect()
    assert profile.tier in {"nvidia", "intel", "amd", "sw"}
    assert profile.encoder in {"h264_nvenc", "h264_qsv", "h264_amf", "libx264"}
    assert profile.ffmpeg_path
    assert profile.ffmpeg_version  # non-empty
    # libx264 is the universal fallback — it MUST be in the encoder list.
    assert "libx264" in profile.available_encoders
