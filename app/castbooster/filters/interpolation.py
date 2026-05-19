"""RIFEFilter — frame interpolation via rife-ncnn-vulkan (P3.2).

Public surface:
  RIFEFilter(source_fps, target_fps, width, height, model="rife-anime",
             rife_path=None, ffmpeg_path=None)
    .render() -> "null"               (no in-band -vf; rife runs out-of-band)
    .pipeline_spec() -> PipelineSpec  (P3.3 _ProcessSlot consumes this)
    ._side_task(ctx) -> None          (synchronous orchestration loop)

Architecture: see docs/superpowers/specs/2026-05-19-pillar-3-frame-interpolation-design.md
§3.2 and §3.3. _side_task spawns a decode ffmpeg, batches 2s windows of
rawvideo into PNG folders, runs rife folder-mode, reads PNGs back as
rawvideo, writes to ctx.encoder_stdin. Cancellable via ctx.cancel_event.
Raises on subprocess failure so _ProcessSlot (P3.3) marks the slot FAILED.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from castbooster import license as _license
from castbooster.ffmpeg_probe import locate_ffmpeg
from castbooster.pipeline_spec import PipelineSpec, SideTaskContext

log = logging.getLogger(__name__)

# Batch duration in seconds (per parent spec D1).
BATCH_SECONDS: float = 2.0


class RIFEFilter:
    """source_fps -> target_fps frame interpolation via rife-ncnn-vulkan folder mode."""

    def __init__(
        self,
        source_fps: float,
        target_fps: int,
        width: int,
        height: int,
        model: str = "rife-anime",
        rife_path: Optional[str] = None,
        ffmpeg_path: Optional[str] = None,
    ) -> None:
        self._source_fps = float(source_fps)
        self._target_fps = int(target_fps)
        self._w = int(width)
        self._h = int(height)
        self._model = model
        self._rife_path = rife_path if rife_path is not None else _license._locate_rife()
        self._ffmpeg_path = ffmpeg_path if ffmpeg_path is not None else locate_ffmpeg()

    def render(self) -> str:
        """rife output is fed to the encoder via rawvideo stdin, not via -vf."""
        return "null"

    def pipeline_spec(self) -> PipelineSpec:
        return PipelineSpec(
            encoder_input_format=f"rawvideo:yuv420p:{self._w}x{self._h}",
            target_fps=self._target_fps,
            side_task_factory=self._side_task,
        )

    def _side_task(self, ctx: SideTaskContext) -> None:
        """Orchestrates decode -> rife -> encoder. Implemented in Tasks 5-6."""
        raise NotImplementedError("RIFEFilter._side_task lands in P3.2 Tasks 5-6")
