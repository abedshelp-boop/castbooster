"""Probe ffmpeg capabilities at startup.

Locates the ffmpeg binary, enumerates the hardware-acceleration paths and H.264
encoders the build supports, then runs a tiny 1-frame encode test against each
candidate to pick the first one that ACTUALLY works on this machine. Result is
cached per-process via lru_cache.

The encoder runtime test is the key defense against ffmpeg listing encoders
that compile-time exist but fail at runtime (e.g. listing h264_nvenc on a
machine with no NVIDIA driver, which happens on Abed's Snapdragon dev box).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


def parse_hwaccels(output: str) -> List[str]:
    """Parse `ffmpeg -hwaccels` stdout into a list of accel method names.

    Format (canonical):
        Hardware acceleration methods:
        cuda
        vaapi
        ...
    First non-blank line ends with ':' — that's the header, skip it.
    """
    out: List[str] = []
    for line in (output or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.endswith(":"):
            continue
        out.append(s)
    return out
