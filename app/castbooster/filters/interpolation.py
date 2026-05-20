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
        model: str = "rife-v4.6",
        rife_path: Optional[str] = None,
        ffmpeg_path: Optional[str] = None,
    ) -> None:
        self._source_fps = float(source_fps)
        self._target_fps = int(target_fps)
        self._w = int(width)
        self._h = int(height)
        # Resolve the model to an absolute path. rife-ncnn-vulkan resolves a
        # bare model name relative to the rife BINARY's directory, not the
        # current working dir — so we ship models in
        # castbooster/models/<name>/ and resolve here, not at the upstream
        # default. A path-like model (contains a separator) is taken as-is.
        if "/" in model or "\\" in model or Path(model).is_absolute():
            self._model = model
        else:
            self._model = str((_license._BUNDLED_MODELS_DIR / model).resolve())
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
        """Orchestrate decode -> batch -> rife -> encoder.

        Runs synchronously on a thread spawned by P3.3's _ProcessSlot.
        Checks ctx.cancel_event in the inner loop; returns promptly when set.
        Raises on subprocess failure so the slot can mark FAILED.
        """
        frame_bytes = self._w * self._h * 3 // 2
        frames_per_input_batch = int(round(self._source_fps * BATCH_SECONDS))
        frames_per_output_batch = int(round(self._target_fps * BATCH_SECONDS))
        input_chunk_size = frame_bytes * frames_per_input_batch

        decode = self._spawn_decode(ctx.input_url)
        batch_idx = 0
        try:
            while not ctx.cancel_event.is_set():
                chunk = self._read_exact(decode.stdout, input_chunk_size)
                if chunk is None or len(chunk) < input_chunk_size:
                    # EOF or short read at end of stream -> stop cleanly.
                    break

                in_dir = ctx.workdir / "in" / str(batch_idx)
                out_dir = ctx.workdir / "out" / str(batch_idx)
                in_dir.mkdir(parents=True, exist_ok=True)
                out_dir.mkdir(parents=True, exist_ok=True)

                self._rawvideo_chunk_to_pngs(chunk, frames_per_input_batch, in_dir)
                self._run_rife(in_dir, out_dir, frames_per_output_batch)
                output_chunk = self._pngs_to_rawvideo_chunk(out_dir, frames_per_output_batch)
                ctx.encoder_stdin.write(output_chunk)

                # rmtree lag = 1: when we finish batch N, drop batch (N-2)'s dirs.
                # batch N-1's dirs stay around until the next iteration.
                self._cleanup_batch_dir(ctx.workdir, batch_idx - 2)

                batch_idx += 1
        finally:
            # Terminate decode if still running (cancel or our raise).
            try:
                if decode.poll() is None:
                    decode.terminate()
                    try:
                        decode.wait(timeout=2.0)
                    except Exception:
                        pass
            except Exception:
                log.exception("decode terminate failed")
            # Cleanup remaining batch dirs.
            self._cleanup_batch_dir(ctx.workdir, batch_idx - 2)
            self._cleanup_batch_dir(ctx.workdir, batch_idx - 1)

        # Decode failure detection: non-zero exit when we did NOT cancel and
        # we did NOT stop because of an upstream EOF that we asked for.
        rc = decode.poll()
        if rc is not None and rc != 0 and not ctx.cancel_event.is_set():
            stderr_bytes = b""
            if decode.stderr is not None:
                try:
                    stderr_bytes = decode.stderr.read() or b""
                except Exception:
                    pass
            raise RuntimeError(
                f"decode ffmpeg exited {rc}: "
                f"{stderr_bytes.decode('utf-8', 'replace')[:500] if stderr_bytes else ''}"
            )

    @staticmethod
    def _read_exact(stream, n: int) -> Optional[bytes]:
        """Read exactly n bytes from a binary stream, or whatever is left at EOF.

        Returns None if the stream is closed; returns a possibly-short bytes
        object at EOF.
        """
        if stream is None:
            return None
        buf = bytearray()
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)
