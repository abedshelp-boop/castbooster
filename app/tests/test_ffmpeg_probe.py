"""Unit tests for castbooster.ffmpeg_probe."""
from pathlib import Path

import pytest

from castbooster import ffmpeg_probe


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_hwaccels_extracts_method_names():
    out = _load("ffmpeg_hwaccels.txt")
    result = ffmpeg_probe.parse_hwaccels(out)
    assert result == [
        "cuda", "vaapi", "dxva2", "qsv", "d3d11va",
        "opencl", "vulkan", "d3d12va", "amf",
    ]


def test_parse_hwaccels_handles_empty_input():
    assert ffmpeg_probe.parse_hwaccels("") == []
    assert ffmpeg_probe.parse_hwaccels("Hardware acceleration methods:\n") == []


def test_parse_encoders_returns_h264_candidates_in_order():
    out = _load("ffmpeg_encoders.txt")
    result = ffmpeg_probe.parse_encoders(out)
    assert "h264_amf" in result
    assert "h264_nvenc" in result
    assert "h264_qsv" in result
    assert "libx264" in result
    # Should NOT include the legend lines ("Video", "Audio", "Subtitle").
    assert "Video" not in result
    assert "=" not in result


def test_parse_encoders_includes_non_h264():
    # Sanity: the function returns every encoder, not just H.264. Caller filters.
    out = _load("ffmpeg_encoders.txt")
    result = ffmpeg_probe.parse_encoders(out)
    assert "aac" in result
    assert "av1_nvenc" in result


def test_parse_encoders_handles_no_table():
    # Output without the "------" divider should yield empty list.
    out = "Encoders:\n V..... = Video\n"
    assert ffmpeg_probe.parse_encoders(out) == []


def test_parse_encoders_handles_empty_input():
    assert ffmpeg_probe.parse_encoders("") == []
