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
