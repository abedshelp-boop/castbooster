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

# The non-`logging` imports below (os, shutil, subprocess, sys, dataclass, field,
# lru_cache, Path, Optional) are intentionally added up-front for the helpers
# that land in upcoming P2.1 sub-tasks (locate_ffmpeg, try_encoder, detect,
# AccelProfile). Removing them here would just force the next task's diff to
# re-add them — keeping the import block stable across tasks reduces noise.
# noqa: F401 directives are not needed because every name below is referenced
# by the time the file reaches its final P2.1 state.
import logging
import os                                                                # noqa: F401  (used by locate_ffmpeg, Task 6)
import shutil                                                            # noqa: F401  (used by locate_ffmpeg, Task 6)
import subprocess                                                        # noqa: F401  (used by try_encoder + _run, Task 7)
import sys                                                               # noqa: F401  (used by _run, Task 7)
from dataclasses import dataclass, field                                 # noqa: F401  (used by AccelProfile, Task 8)
from functools import lru_cache                                          # noqa: F401  (used by detect, Task 8)
from pathlib import Path                                                 # noqa: F401  (used by locate_ffmpeg, Task 6)
from typing import List, Optional                                        # noqa: F401  (used by locate_ffmpeg, Task 6)

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


def parse_encoders(output: str) -> List[str]:
    """Parse `ffmpeg -encoders` stdout into a list of encoder names.

    Format (canonical):
        Encoders:
         V..... = Video
         A..... = Audio
         ...
         ------
         V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
         V..... h264_qsv             H.264 / AVC / MPEG-4 AVC ...
         ...
    Each entry line has a 6-char flag block, then the encoder name, then
    a free-form description. We only process lines AFTER the `------`
    divider so the legend rows above it don't poison the result.
    """
    out: List[str] = []
    in_table = False
    for raw in (output or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("------"):
            in_table = True
            continue
        if not in_table or not stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 2:
            continue
        flags, name = parts[0], parts[1]
        # Real flag blocks are exactly 6 chars and start with V/A/S.
        if len(flags) != 6:
            continue
        if flags[0] not in ("V", "A", "S"):
            continue
        out.append(name)
    return out
