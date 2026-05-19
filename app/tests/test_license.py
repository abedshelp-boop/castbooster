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
