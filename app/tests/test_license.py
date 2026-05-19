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
