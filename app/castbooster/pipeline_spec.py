"""Pipeline specs for FilterStages that need their own process topology.

P3.1 ships the dataclasses only. Consumed in P3.3 by _ProcessSlot.
Produced in P3.2 by RIFEFilter.

A FilterStage returns `None` from pipeline_spec() to mean "pure -vf filter,
today's single-Popen behavior". A real PipelineSpec triggers the multi-process
path that P3.3 will add to _ProcessSlot.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable


@dataclass(frozen=True)
class SideTaskContext:
    """Handle passed to a FilterStage's side-task callable at spawn time.

    The side task runs synchronously on its own threading.Thread (spawned by
    _ProcessSlot in P3.3) and must check `cancel_event` in its inner loop.
    Writing to `encoder_stdin` feeds the encode ffmpeg directly.

    Fields:
        input_url: the upstream HLS / file URL the side task should read from
            (typically by spawning a decode ffmpeg).
        target_fps: output framerate the encoder is configured for. The side
            task must emit frames at this rate (after interpolation).
        encoder_stdin: write rawvideo bytes here. Already opened by
            _ProcessSlot; the side task does NOT close it.
        workdir: scratch directory for batch frame folders; created by
            _ProcessSlot; the side task may create subdirs but must not
            delete the workdir itself.
        cancel_event: set by _ProcessSlot when the slot is being torn down.
            The side task MUST check this in its inner loop and return
            promptly when set.
    """
    input_url: str
    target_fps: int
    encoder_stdin: IO[bytes]
    workdir: Path
    cancel_event: threading.Event


@dataclass(frozen=True)
class PipelineSpec:
    """What a non-trivial FilterStage tells the Transcoder about its pipeline.

    A return value of `None` from FilterStage.pipeline_spec() means "pure -vf
    filter, today's single-Popen behavior". A real PipelineSpec triggers the
    multi-process path in P3.3.

    Fields:
        encoder_input_format: how to configure the encode ffmpeg's stdin.
            Format string: "rawvideo:<pix_fmt>:<W>x<H>", e.g.
            "rawvideo:yuv420p:1920x1080".
        target_fps: output framerate. Encoder gets -r and a sqrt-scaled
            bitrate (per parent spec D6, implemented in P3.3).
        side_task_factory: synchronous callable invoked on a fresh thread
            with a SideTaskContext. Raises on subprocess failure so
            _ProcessSlot can mark the slot FAILED.
    """
    encoder_input_format: str
    target_fps: int
    side_task_factory: Callable[[SideTaskContext], None]
