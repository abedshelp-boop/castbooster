"""Unit tests for castbooster.ffmpeg_probe."""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

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


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    """Build a CompletedProcess-shaped MagicMock for monkeypatching _run."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_try_encoder_returns_true_on_success(monkeypatch):
    monkeypatch.setattr(
        ffmpeg_probe, "_run", lambda args, timeout: _completed(0)
    )
    assert ffmpeg_probe.try_encoder("ffmpeg.exe", "libx264") is True


def test_try_encoder_returns_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        ffmpeg_probe, "_run",
        lambda args, timeout: _completed(1, stderr="no NVIDIA driver"),
    )
    assert ffmpeg_probe.try_encoder("ffmpeg.exe", "h264_nvenc") is False


def test_try_encoder_returns_false_on_timeout(monkeypatch):
    def _raise(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5)
    monkeypatch.setattr(ffmpeg_probe, "_run", _raise)
    assert ffmpeg_probe.try_encoder("ffmpeg.exe", "h264_qsv") is False


def test_try_encoder_returns_false_on_unexpected_exception(monkeypatch):
    def _raise(*_a, **_kw):
        raise OSError("file not found")
    monkeypatch.setattr(ffmpeg_probe, "_run", _raise)
    assert ffmpeg_probe.try_encoder("ffmpeg.exe", "libx264") is False


def test_accel_profile_is_immutable():
    p = ffmpeg_probe.AccelProfile(
        ffmpeg_path="ff", encoder="libx264", decoder="d3d11va",
        tier="sw", available_encoders=[], available_hwaccels=[],
        ffmpeg_version="",
    )
    # dataclasses.FrozenInstanceError is a subclass of AttributeError, but
    # asserting on AttributeError keeps the test robust across Python versions.
    with pytest.raises(AttributeError):
        p.encoder = "h264_nvenc"  # type: ignore[misc]


def test_detect_picks_libx264_when_only_sw_works(monkeypatch, tmp_path):
    # Stub locate_ffmpeg to return a fake path.
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(ffmpeg_probe, "locate_ffmpeg", lambda override=None: str(fake))
    # Stub enumeration: -hwaccels + -encoders + -version. Stub try_encoder to
    # fail every HW encoder, succeed only libx264.
    def _stub_run(args, timeout):
        if "-version" in args:
            return _completed(0, stdout="ffmpeg version 8.1-test\n")
        if "-hwaccels" in args:
            return _completed(0, stdout=_load("ffmpeg_hwaccels.txt"))
        if "-encoders" in args:
            return _completed(0, stdout=_load("ffmpeg_encoders.txt"))
        raise AssertionError(f"unexpected args: {args}")
    monkeypatch.setattr(ffmpeg_probe, "_run", _stub_run)
    monkeypatch.setattr(
        ffmpeg_probe, "try_encoder",
        lambda path, enc: enc == "libx264",
    )
    # Clear the lru_cache before each call so monkeypatches take effect.
    ffmpeg_probe.detect.cache_clear()
    profile = ffmpeg_probe.detect()
    assert profile.encoder == "libx264"
    assert profile.tier == "sw"
    assert profile.ffmpeg_path == str(fake)
    assert profile.decoder == "d3d11va"   # fixtures list d3d11va, sw tier prefers it
    assert "h264_nvenc" in profile.available_encoders   # enumeration still recorded
    assert "cuda" in profile.available_hwaccels


def test_detect_picks_nvenc_when_available(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(ffmpeg_probe, "locate_ffmpeg", lambda override=None: str(fake))
    def _stub_run(args, timeout):
        if "-version" in args:
            return _completed(0, stdout="ffmpeg version 8.1-test\n")
        if "-hwaccels" in args:
            return _completed(0, stdout=_load("ffmpeg_hwaccels.txt"))
        if "-encoders" in args:
            return _completed(0, stdout=_load("ffmpeg_encoders.txt"))
        raise AssertionError
    monkeypatch.setattr(ffmpeg_probe, "_run", _stub_run)
    # All HW encoders "work" in this scenario.
    monkeypatch.setattr(ffmpeg_probe, "try_encoder", lambda path, enc: True)
    ffmpeg_probe.detect.cache_clear()
    profile = ffmpeg_probe.detect()
    assert profile.encoder == "h264_nvenc"   # first in priority
    assert profile.tier == "nvidia"
    assert profile.decoder == "cuda"


def test_detect_raises_when_every_encoder_fails(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(ffmpeg_probe, "locate_ffmpeg", lambda override=None: str(fake))
    def _stub_run(args, timeout):
        if "-version" in args:
            return _completed(0, stdout="ffmpeg version 8.1-test\n")
        if "-hwaccels" in args:
            return _completed(0, stdout="Hardware acceleration methods:\n")
        if "-encoders" in args:
            return _completed(0, stdout=_load("ffmpeg_encoders.txt"))
        raise AssertionError
    monkeypatch.setattr(ffmpeg_probe, "_run", _stub_run)
    monkeypatch.setattr(ffmpeg_probe, "try_encoder", lambda path, enc: False)
    ffmpeg_probe.detect.cache_clear()
    with pytest.raises(ffmpeg_probe.FFmpegProbeError):
        ffmpeg_probe.detect()


def test_detect_caches_result(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(ffmpeg_probe, "locate_ffmpeg", lambda override=None: str(fake))
    call_count = {"n": 0}
    def _stub_run(args, timeout):
        call_count["n"] += 1
        if "-version" in args:
            return _completed(0, stdout="ffmpeg version 8.1-test\n")
        if "-hwaccels" in args:
            return _completed(0, stdout=_load("ffmpeg_hwaccels.txt"))
        if "-encoders" in args:
            return _completed(0, stdout=_load("ffmpeg_encoders.txt"))
        return _completed(0)
    monkeypatch.setattr(ffmpeg_probe, "_run", _stub_run)
    monkeypatch.setattr(ffmpeg_probe, "try_encoder", lambda path, enc: enc == "libx264")
    ffmpeg_probe.detect.cache_clear()
    ffmpeg_probe.detect()
    after_first = call_count["n"]
    ffmpeg_probe.detect()
    assert call_count["n"] == after_first, "detect() should be cached"
