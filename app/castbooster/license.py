"""Pro-tier license check + Vulkan capability probe.

This is a deliberately minimal module:
- `is_pro()` is a P3 stub that returns True. P6 wires real Polar.sh JWT
  validation here without changing the public surface.
- `vulkan_available()` is a one-shot probe (cached for the process lifetime)
  that confirms `rife-ncnn-vulkan` is locatable and its Vulkan ICD loads.
- `_locate_rife()` mirrors `ffmpeg_probe.locate_ffmpeg()` — override → env
  var → bundled → PATH.
"""
from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


class RIFENotFoundError(RuntimeError):
    """No rife-ncnn-vulkan binary could be located."""


def is_pro() -> bool:
    """Stub for P3. P6 wires real Polar.sh JWT validation here."""
    return True


_BUNDLED_RIFE = Path(__file__).parent / "bin" / "rife-ncnn-vulkan.exe"


def _locate_rife(override: Optional[str] = None) -> str:
    """Find rife-ncnn-vulkan in priority order: override arg, env, bundled, PATH.

    Returns the absolute path. Raises RIFENotFoundError if none of the
    candidates point at a real file. Mirrors ffmpeg_probe.locate_ffmpeg().
    """
    candidates: List[Optional[str]] = []
    if override:
        candidates.append(override)
    env = os.environ.get("CASTBOOSTER_RIFE")
    if env:
        candidates.append(env)
    candidates.append(str(_BUNDLED_RIFE))
    path_lookup = shutil.which("rife-ncnn-vulkan")
    if path_lookup:
        candidates.append(path_lookup)
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c).resolve())
    raise RIFENotFoundError(
        f"rife-ncnn-vulkan not found. Tried: {candidates}. "
        f"Run app/scripts/fetch_rife.ps1 to download it."
    )
