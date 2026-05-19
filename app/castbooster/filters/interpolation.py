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
import shutil
import subprocess
import sys
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

    def _spawn_decode(self, input_url: str) -> subprocess.Popen:
        """Spawn ffmpeg-decode reading upstream HLS, writing rawvideo to stdout.

        Audio is dropped (-an) since rife only sees the video stream; the
        primary encode ffmpeg will re-attach audio when P3.3 lands.
        """
        argv = [
            self._ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-i", input_url,
            "-an",
            "-f", "rawvideo",
            "-pix_fmt", "yuv420p",
            "-",
        ]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        log.info("decode ffmpeg argv: %s", " ".join(repr(a) for a in argv))
        return subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )

    def _rawvideo_chunk_to_pngs(
        self,
        chunk: bytes,
        frame_count: int,
        in_dir: Path,
    ) -> None:
        """Convert a rawvideo chunk of `frame_count` yuv420p frames to PNGs in in_dir.

        Uses a per-batch short-lived ffmpeg. Raises RuntimeError on non-zero exit.
        """
        argv = [
            self._ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-f", "rawvideo",
            "-pix_fmt", "yuv420p",
            "-video_size", f"{self._w}x{self._h}",
            "-framerate", str(int(round(self._source_fps))),
            "-i", "-",
            "-frames:v", str(frame_count),
            "-y",
            str(in_dir / "%08d.png"),
        ]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        _, stderr = proc.communicate(input=chunk, timeout=60.0)
        if proc.returncode != 0:
            raise RuntimeError(
                f"raw->PNG ffmpeg exited {proc.returncode}: "
                f"{stderr.decode('utf-8', 'replace')[:500] if stderr else ''}"
            )

    def _run_rife(self, in_dir: Path, out_dir: Path, target_count: int) -> None:
        """Invoke rife-ncnn-vulkan folder mode. Raises RuntimeError on non-zero.

        Argv shape (P3.2 — Pillar 5 will add -g):
            rife-ncnn-vulkan -i <in_dir> -o <out_dir> -n <target_count>
                             -m <model>
        """
        argv = [
            self._rife_path,
            "-i", str(in_dir),
            "-o", str(out_dir),
            "-n", str(target_count),
            "-m", self._model,
        ]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        log.info("rife argv: %s", " ".join(repr(a) for a in argv))
        result = subprocess.run(
            argv,
            capture_output=True,
            timeout=300.0,
            creationflags=creationflags,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace") if result.stderr else ""
            raise RuntimeError(
                f"rife-ncnn-vulkan exited {result.returncode}: {stderr[:500]}"
            )

    def _pngs_to_rawvideo_chunk(self, out_dir: Path, frame_count: int) -> bytes:
        """Convert PNGs at out_dir/%08d.png to rawvideo yuv420p bytes.

        Raises RuntimeError on non-zero exit.
        """
        argv = [
            self._ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-framerate", str(self._target_fps),
            "-i", str(out_dir / "%08d.png"),
            "-frames:v", str(frame_count),
            "-f", "rawvideo",
            "-pix_fmt", "yuv420p",
            "-",
        ]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        stdout, stderr = proc.communicate(timeout=120.0)
        if proc.returncode != 0:
            raise RuntimeError(
                f"PNG->raw ffmpeg exited {proc.returncode}: "
                f"{stderr.decode('utf-8', 'replace')[:500] if stderr else ''}"
            )
        return stdout

    def _cleanup_batch_dir(self, workdir: Path, batch_idx: int) -> None:
        """Remove workdir/in/<batch_idx> and workdir/out/<batch_idx> if present.

        No-op on missing dirs or negative index (used for the very first batch
        where there is no predecessor to clean up).
        """
        if batch_idx < 0:
            return
        for sub in ("in", "out"):
            d = workdir / sub / str(batch_idx)
            try:
                shutil.rmtree(d)
            except FileNotFoundError:
                pass
            except OSError:
                log.exception("rmtree of %s failed (will retry later)", d)

    def _side_task(self, ctx: SideTaskContext) -> None:
        """Orchestrates decode -> rife -> encoder. Implemented in Tasks 5-6."""
        raise NotImplementedError("RIFEFilter._side_task lands in P3.2 Tasks 5-6")
