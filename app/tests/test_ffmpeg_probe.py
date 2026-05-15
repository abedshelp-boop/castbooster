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
