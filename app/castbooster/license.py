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


_VULKAN_ERROR_KEYWORDS = (
    "vulkan",            # generic vulkan ICD errors
    "no gpu",
    "no vulkan device",
)
_PROBE_TIMEOUT = 5.0  # seconds


@functools.cache
def vulkan_available() -> bool:
    """One-shot probe: True iff rife-ncnn-vulkan launches and reports no Vulkan errors.

    Strategy:
      1. Locate the binary via _locate_rife(). Missing -> False (do NOT raise).
      2. Run it with `-h` (help) to force ICD load without doing any inference.
      3. Treat as False on: subprocess exception, non-zero exit, OR stderr
         mentioning vulkan error keywords (since rife's `-h` exit code on
         Vulkan ICD failure is undocumented).

    Cached for the process lifetime via @functools.cache. Tests call
    vulkan_available.cache_clear() between scenarios.
    """
    try:
        rife = _locate_rife()
    except RIFENotFoundError:
        log.info("vulkan_available: rife binary not located")
        return False
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [rife, "-h"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            creationflags=creationflags,
        )
    except FileNotFoundError:
        log.info("vulkan_available: rife binary disappeared between locate and run")
        return False
    except subprocess.TimeoutExpired:
        log.warning("vulkan_available: rife -h timed out after %ss", _PROBE_TIMEOUT)
        return False
    except Exception:
        log.exception("vulkan_available: rife -h crashed")
        return False
    if result.returncode != 0:
        log.info(
            "vulkan_available: rife -h exit=%d stderr=%r",
            result.returncode, (result.stderr or "")[:200],
        )
        return False
    stderr_lower = (result.stderr or "").lower()
    if any(kw in stderr_lower for kw in _VULKAN_ERROR_KEYWORDS):
        # only treat as failure if the keyword appears in an *error* context.
        # rife's normal -h output may mention "vulkan" in flag descriptions,
        # so we look for explicit failure phrases.
        if "failed" in stderr_lower or "error" in stderr_lower or "not found" in stderr_lower:
            log.info(
                "vulkan_available: rife stderr signals Vulkan failure: %r",
                (result.stderr or "")[:200],
            )
            return False
    return True
