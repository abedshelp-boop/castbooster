"""Unit tests for castbooster.ffmpeg_probe video-probing surface.

Tests _locate_ffprobe, _parse_rational_fps, InputVideoInfo, and
probe_input_video. Mocks subprocess via monkeypatching _run; never invokes
real ffprobe (the gated integration test lives in P3.2's RIFE work).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from castbooster import ffmpeg_probe


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    """Build a CompletedProcess-shaped MagicMock for monkeypatching _run."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ---------- _parse_rational_fps (V12) ---------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("24000/1001", pytest.approx(23.976, rel=1e-3)),
    ("30/1", 30.0),
    ("60000/1001", pytest.approx(59.94, rel=1e-3)),
    ("25/1", 25.0),
])
def test_parse_rational_fps_returns_float_for_valid_input(value, expected):
    assert ffmpeg_probe._parse_rational_fps(value) == expected


@pytest.mark.parametrize("value", [
    "0/0",         # ffprobe's "unknown" sentinel — common on HLS streams
    "0/1",         # zero numerator → fps=0 → not useful → None
    "",            # empty string
    "abc",         # not a fraction
    "24",          # missing denominator
    "24/",         # missing denominator
    "/30",         # missing numerator
    "24/0",        # division by zero
    "-30/1",       # negative — not a valid fps
    "30/-1",       # negative denominator — also invalid
])
def test_parse_rational_fps_returns_none_for_invalid_input(value):
    assert ffmpeg_probe._parse_rational_fps(value) is None


# ---------- _locate_ffprobe (V11) -------------------------------------------

def test_locate_ffprobe_derives_from_ffmpeg_path(tmp_path, monkeypatch):
    """Given an ffmpeg path, returns the sibling ffprobe path if it exists."""
    monkeypatch.setattr(sys, "platform", "win32")
    ffmpeg_exe = tmp_path / "ffmpeg.exe"
    ffmpeg_exe.write_bytes(b"x")
    ffprobe_exe = tmp_path / "ffprobe.exe"
    ffprobe_exe.write_bytes(b"x")
    assert ffmpeg_probe._locate_ffprobe(str(ffmpeg_exe)) == str(ffprobe_exe)


def test_locate_ffprobe_returns_none_when_sibling_missing(tmp_path, monkeypatch):
    """Returns None if ffprobe is not next to ffmpeg."""
    monkeypatch.setattr(sys, "platform", "win32")
    ffmpeg_exe = tmp_path / "ffmpeg.exe"
    ffmpeg_exe.write_bytes(b"x")
    # Note: no ffprobe.exe sibling.
    assert ffmpeg_probe._locate_ffprobe(str(ffmpeg_exe)) is None


def test_locate_ffprobe_uses_unix_name_on_non_windows(tmp_path, monkeypatch):
    """On Linux/macOS the sibling is `ffprobe`, not `ffprobe.exe`."""
    monkeypatch.setattr(sys, "platform", "linux")
    ffmpeg_bin = tmp_path / "ffmpeg"
    ffmpeg_bin.write_bytes(b"x")
    ffprobe_bin = tmp_path / "ffprobe"
    ffprobe_bin.write_bytes(b"x")
    assert ffmpeg_probe._locate_ffprobe(str(ffmpeg_bin)) == str(ffprobe_bin)


# ---------- InputVideoInfo (V10) --------------------------------------------

def test_input_video_info_is_immutable():
    """InputVideoInfo is frozen — assignment raises AttributeError."""
    info = ffmpeg_probe.InputVideoInfo(
        fps=23.976, width=1920, height=1080, pix_fmt="yuv420p",
    )
    with pytest.raises(AttributeError):
        info.fps = 30.0  # type: ignore[misc]


def test_input_video_info_holds_all_fields():
    """Constructor accepts all 4 documented fields."""
    info = ffmpeg_probe.InputVideoInfo(
        fps=None, width=640, height=360, pix_fmt="yuv420p",
    )
    assert info.fps is None
    assert info.width == 640
    assert info.height == 360
    assert info.pix_fmt == "yuv420p"


# ---------- probe_input_video happy path (V1) -------------------------------

def test_probe_returns_info_for_cfr_input(tmp_path, monkeypatch):
    """CFR input (r == a) → returns InputVideoInfo with parsed fps."""
    # Stub _locate_ffprobe to return a known path so we don't depend on the
    # filesystem layout. Stub _run to return the CFR fixture JSON.
    monkeypatch.setattr(
        ffmpeg_probe, "_locate_ffprobe",
        lambda ffmpeg_path: "fake-ffprobe",
    )
    monkeypatch.setattr(
        ffmpeg_probe, "_run",
        lambda args, timeout: _completed(0, stdout=_load("ffprobe_cfr_1080p.json")),
    )
    info = ffmpeg_probe.probe_input_video(
        "http://example.com/x.m3u8",
        ffmpeg_path="fake-ffmpeg",
    )
    assert info is not None
    assert info.fps == pytest.approx(23.976, rel=1e-3)
    assert info.width == 1920
    assert info.height == 1080
    assert info.pix_fmt == "yuv420p"
