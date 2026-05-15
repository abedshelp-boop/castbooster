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


# Tests for locate_ffmpeg below need `shutil` access for monkeypatching.
def test_locate_uses_explicit_override(tmp_path):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"not a real binary")
    result = ffmpeg_probe.locate_ffmpeg(override=str(fake))
    assert result == str(fake.resolve())


def test_locate_uses_env_var(tmp_path, monkeypatch):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"x")
    monkeypatch.setenv("CASTBOOSTER_FFMPEG", str(fake))
    # Make sure PATH lookup also points at fake, so we don't accidentally
    # pass via a different fallback.
    monkeypatch.setattr(ffmpeg_probe.shutil, "which", lambda _: None)
    # Bundled path must not exist either — patch the module-level constant.
    monkeypatch.setattr(ffmpeg_probe, "_BUNDLED_FFMPEG", tmp_path / "nope.exe")
    assert ffmpeg_probe.locate_ffmpeg() == str(fake.resolve())


def test_locate_uses_bundled(tmp_path, monkeypatch):
    fake = tmp_path / "bundled" / "ffmpeg.exe"
    fake.parent.mkdir()
    fake.write_bytes(b"x")
    monkeypatch.delenv("CASTBOOSTER_FFMPEG", raising=False)
    monkeypatch.setattr(ffmpeg_probe, "_BUNDLED_FFMPEG", fake)
    monkeypatch.setattr(ffmpeg_probe.shutil, "which", lambda _: None)
    assert ffmpeg_probe.locate_ffmpeg() == str(fake.resolve())


def test_locate_falls_back_to_path(tmp_path, monkeypatch):
    fake = tmp_path / "path_ffmpeg.exe"
    fake.write_bytes(b"x")
    monkeypatch.delenv("CASTBOOSTER_FFMPEG", raising=False)
    monkeypatch.setattr(ffmpeg_probe, "_BUNDLED_FFMPEG", tmp_path / "nope.exe")
    monkeypatch.setattr(
        ffmpeg_probe.shutil, "which",
        lambda name: str(fake) if name == "ffmpeg" else None,
    )
    assert ffmpeg_probe.locate_ffmpeg() == str(fake.resolve())


def test_locate_raises_when_nothing_found(monkeypatch, tmp_path):
    monkeypatch.delenv("CASTBOOSTER_FFMPEG", raising=False)
    monkeypatch.setattr(ffmpeg_probe, "_BUNDLED_FFMPEG", tmp_path / "nope.exe")
    monkeypatch.setattr(ffmpeg_probe.shutil, "which", lambda _: None)
    with pytest.raises(ffmpeg_probe.FFmpegNotFoundError):
        ffmpeg_probe.locate_ffmpeg()
