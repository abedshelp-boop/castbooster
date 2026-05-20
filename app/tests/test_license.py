"""Unit tests for castbooster.license.

is_pro() is a P3 stub; P6 wires real Polar.sh validation.
vulkan_available() is a one-shot capability probe.
_locate_rife() mirrors ffmpeg_probe.locate_ffmpeg().
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_is_pro_returns_true_stub():
    """P3 stub: always True. P6 will replace with Polar.sh JWT check."""
    from castbooster.license import is_pro
    assert is_pro() is True


def test_rife_not_found_error_is_runtime_error():
    """RIFENotFoundError must subclass RuntimeError so callers can catch broadly."""
    from castbooster.license import RIFENotFoundError
    assert issubclass(RIFENotFoundError, RuntimeError)


# ---------- _locate_rife ----------------------------------------------------

def test_locate_rife_uses_explicit_override(tmp_path):
    """Override argument wins over all other candidates."""
    from castbooster import license as lic
    fake = tmp_path / "rife-ncnn-vulkan.exe"
    fake.write_bytes(b"not a real binary")
    result = lic._locate_rife(override=str(fake))
    assert result == str(fake.resolve())


def test_locate_rife_uses_env_var(tmp_path, monkeypatch):
    """CASTBOOSTER_RIFE env var is consulted after override."""
    from castbooster import license as lic
    fake = tmp_path / "rife.exe"
    fake.write_bytes(b"x")
    monkeypatch.setenv("CASTBOOSTER_RIFE", str(fake))
    monkeypatch.setattr(lic.shutil, "which", lambda _: None)
    monkeypatch.setattr(lic, "_BUNDLED_RIFE", tmp_path / "nope.exe")
    assert lic._locate_rife() == str(fake.resolve())


def test_locate_rife_uses_bundled(tmp_path, monkeypatch):
    """Bundled location is the third candidate (after override + env)."""
    from castbooster import license as lic
    fake = tmp_path / "rife-ncnn-vulkan.exe"
    fake.write_bytes(b"x")
    monkeypatch.delenv("CASTBOOSTER_RIFE", raising=False)
    monkeypatch.setattr(lic.shutil, "which", lambda _: None)
    monkeypatch.setattr(lic, "_BUNDLED_RIFE", fake)
    assert lic._locate_rife() == str(fake.resolve())


def test_locate_rife_falls_back_to_path(tmp_path, monkeypatch):
    """PATH lookup is the last candidate."""
    from castbooster import license as lic
    fake = tmp_path / "rife.exe"
    fake.write_bytes(b"x")
    monkeypatch.delenv("CASTBOOSTER_RIFE", raising=False)
    monkeypatch.setattr(lic, "_BUNDLED_RIFE", tmp_path / "nope.exe")
    monkeypatch.setattr(lic.shutil, "which", lambda name: str(fake) if "rife" in name else None)
    assert lic._locate_rife() == str(fake.resolve())


def test_locate_rife_raises_when_nothing_found(tmp_path, monkeypatch):
    """RIFENotFoundError when every candidate is absent."""
    from castbooster import license as lic
    monkeypatch.delenv("CASTBOOSTER_RIFE", raising=False)
    monkeypatch.setattr(lic, "_BUNDLED_RIFE", tmp_path / "nope.exe")
    monkeypatch.setattr(lic.shutil, "which", lambda _: None)
    with pytest.raises(lic.RIFENotFoundError):
        lic._locate_rife()


# ---------- vulkan_available ------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_vulkan_cache():
    """vulkan_available is @functools.cache'd — clear it between tests."""
    from castbooster import license as lic
    lic.vulkan_available.cache_clear()
    yield
    lic.vulkan_available.cache_clear()


_REAL_RIFE_HELP_STDERR = (
    "Usage: rife-ncnn-vulkan -0 infile -1 infile1 -o outfile [options]...\n"
    "       rife-ncnn-vulkan -i indir -o outdir [options]...\n"
    "\n  -h                   show this help\n"
)


def test_vulkan_available_true_on_real_rife_help_output(tmp_path, monkeypatch):
    """rife-ncnn-vulkan 20221029 exits 127 on -h but writes usage to stderr.

    The probe must recognise that as success (Vulkan ICD loaded; help printed)
    and ignore the non-zero exit code.
    """
    from castbooster import license as lic
    fake = tmp_path / "rife-ncnn-vulkan.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(lic, "_BUNDLED_RIFE", fake)
    monkeypatch.delenv("CASTBOOSTER_RIFE", raising=False)

    def _fake_run(args, **kw):
        return subprocess.CompletedProcess(
            args=args, returncode=127,
            stdout="", stderr=_REAL_RIFE_HELP_STDERR,
        )
    monkeypatch.setattr(lic.subprocess, "run", _fake_run)
    assert lic.vulkan_available() is True


def test_vulkan_available_false_on_silent_crash(tmp_path, monkeypatch):
    """Subprocess returns non-zero AND produces no recognisable output -> False.

    This rules out a broken-binary scenario where rife crashes silently before
    the help string is emitted (e.g. corrupted exe).
    """
    from castbooster import license as lic
    fake = tmp_path / "rife-ncnn-vulkan.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(lic, "_BUNDLED_RIFE", fake)
    monkeypatch.delenv("CASTBOOSTER_RIFE", raising=False)

    def _fake_run(args, **kw):
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr="")
    monkeypatch.setattr(lic.subprocess, "run", _fake_run)
    assert lic.vulkan_available() is False


def test_vulkan_available_false_when_binary_missing(tmp_path, monkeypatch):
    """No rife binary → False (and we do not crash with RIFENotFoundError)."""
    from castbooster import license as lic
    monkeypatch.delenv("CASTBOOSTER_RIFE", raising=False)
    monkeypatch.setattr(lic, "_BUNDLED_RIFE", tmp_path / "nope.exe")
    monkeypatch.setattr(lic.shutil, "which", lambda _: None)
    assert lic.vulkan_available() is False


def test_vulkan_available_false_on_filenotfound(tmp_path, monkeypatch):
    """subprocess.run raising FileNotFoundError → False."""
    from castbooster import license as lic
    fake = tmp_path / "rife-ncnn-vulkan.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(lic, "_BUNDLED_RIFE", fake)
    monkeypatch.delenv("CASTBOOSTER_RIFE", raising=False)

    def _raise(*a, **kw):
        raise FileNotFoundError("rife missing")
    monkeypatch.setattr(lic.subprocess, "run", _raise)
    assert lic.vulkan_available() is False


def test_vulkan_available_false_on_timeout(tmp_path, monkeypatch):
    """subprocess.run raising TimeoutExpired → False."""
    from castbooster import license as lic
    fake = tmp_path / "rife-ncnn-vulkan.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(lic, "_BUNDLED_RIFE", fake)
    monkeypatch.delenv("CASTBOOSTER_RIFE", raising=False)

    def _raise(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="rife", timeout=2.0)
    monkeypatch.setattr(lic.subprocess, "run", _raise)
    assert lic.vulkan_available() is False


def test_vulkan_available_false_on_vulkan_error_stderr(tmp_path, monkeypatch):
    """Help text plus Vulkan failure stderr -> False."""
    from castbooster import license as lic
    fake = tmp_path / "rife-ncnn-vulkan.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(lic, "_BUNDLED_RIFE", fake)
    monkeypatch.delenv("CASTBOOSTER_RIFE", raising=False)

    def _fake_run(args, **kw):
        return subprocess.CompletedProcess(
            args=args, returncode=127, stdout="",
            stderr=_REAL_RIFE_HELP_STDERR + "\nfailed to find Vulkan device\n",
        )
    monkeypatch.setattr(lic.subprocess, "run", _fake_run)
    assert lic.vulkan_available() is False


def test_vulkan_available_is_cached(tmp_path, monkeypatch):
    """Repeated calls hit the @functools.cache, not the subprocess."""
    from castbooster import license as lic
    fake = tmp_path / "rife-ncnn-vulkan.exe"
    fake.write_bytes(b"x")
    monkeypatch.setattr(lic, "_BUNDLED_RIFE", fake)
    monkeypatch.delenv("CASTBOOSTER_RIFE", raising=False)

    calls = {"n": 0}

    def _fake_run(args, **kw):
        calls["n"] += 1
        return subprocess.CompletedProcess(
            args=args, returncode=127, stdout="", stderr=_REAL_RIFE_HELP_STDERR,
        )

    monkeypatch.setattr(lic.subprocess, "run", _fake_run)
    assert lic.vulkan_available() is True
    assert lic.vulkan_available() is True
    assert lic.vulkan_available() is True
    assert calls["n"] == 1
