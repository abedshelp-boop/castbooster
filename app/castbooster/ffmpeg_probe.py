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


class FFmpegNotFoundError(RuntimeError):
    """No ffmpeg binary could be located via override, env, bundled, or PATH."""


class FFmpegProbeError(RuntimeError):
    """ffmpeg was located but every H.264 encoder probe failed (libx264 included).

    This should be impossible in practice — libx264 has no hardware dependency.
    Seeing this typically means the vendored binary is corrupt or being blocked
    by AV / EDR.
    """


_BUNDLED_FFMPEG = Path(__file__).parent / "bin" / "ffmpeg.exe"


def locate_ffmpeg(override: Optional[str] = None) -> str:
    """Find ffmpeg in priority order: override arg, env, bundled, PATH.

    Returns the absolute path. Raises FFmpegNotFoundError if none of the
    candidates point at a real file.
    """
    candidates: List[Optional[str]] = []
    if override:
        candidates.append(override)
    env = os.environ.get("CASTBOOSTER_FFMPEG")
    if env:
        candidates.append(env)
    candidates.append(str(_BUNDLED_FFMPEG))
    path_lookup = shutil.which("ffmpeg")
    if path_lookup:
        candidates.append(path_lookup)
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c).resolve())
    raise FFmpegNotFoundError(
        f"ffmpeg not found. Tried: {candidates}. "
        f"Run app/scripts/fetch_ffmpeg.ps1 to download it."
    )


_PROBE_TIMEOUT = 5.0  # seconds per encoder probe


def _run(args: List[str], *, timeout: float) -> subprocess.CompletedProcess:
    """subprocess.run wrapper that suppresses the flashing console window on
    Windows and always captures both stdout and stderr as text."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=creationflags,
    )


def try_encoder(ffmpeg_path: str, encoder: str) -> bool:
    """Run a tiny encode-then-discard test. Returns True iff ffmpeg exits 0.

    Why we do this instead of trusting `-encoders` enumeration: ffmpeg lists
    encoders that compile-time exist but fail at runtime — e.g. it lists
    h264_nvenc even on a machine with no NVIDIA driver. The only way to know
    an encoder REALLY works is to actually try one frame.
    """
    args = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=64x64:rate=1:duration=0.1",
        "-c:v", encoder, "-t", "0.05",
        "-f", "null", "-",
    ]
    try:
        result = _run(args, timeout=_PROBE_TIMEOUT)
        if result.returncode != 0:
            log.info(
                "encoder %s runtime probe failed (exit=%d): %s",
                encoder, result.returncode, (result.stderr or "")[:200],
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        log.warning("encoder %s timed out after %ss", encoder, _PROBE_TIMEOUT)
        return False
    except Exception:
        log.exception("encoder %s probe crashed", encoder)
        return False
