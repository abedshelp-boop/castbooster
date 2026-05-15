"""Per-session ffmpeg subprocess manager with explicit state machine.

State machine (every observable state is enumerated; no predicate ever
treats "not active" as "done" — see 2026-04-22 IDLE-race lesson):

    IDLE         constructed; start() not yet called
    SPAWNING     Popen returned; pipes open; no log line yet
    WARMING      ffmpeg running; output files not yet present
    READY        >= 2 seg_*.ts + master.m3u8 + variant.m3u8 on disk
    STREAMING    new segment appeared after READY (cadence healthy)
    STALLED      STREAMING but no new seg for stall_timeout seconds
    FAILED       subprocess exited non-zero OR stderr matched fatal pattern
                 OR warming_timeout elapsed pre-READY
    TERMINATING  stop() called; q\\n sent to stdin; draining
    TERMINATED   subprocess reaped + output_dir deleted

Threading: one stderr-reader thread + one watchdog-poller thread per
Transcoder instance. State mutations go through _set_state_locked() under
_state_lock. _ready_event is set the first time state becomes READY or
FAILED so wait_until_ready() can block on it cleanly.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from enum import Enum
from pathlib import Path
from typing import Optional

from castbooster.ffmpeg_probe import AccelProfile

log = logging.getLogger(__name__)


class TranscoderState(Enum):
    IDLE = "idle"
    SPAWNING = "spawning"
    WARMING = "warming"
    READY = "ready"
    STREAMING = "streaming"
    STALLED = "stalled"
    FAILED = "failed"
    TERMINATING = "terminating"
    TERMINATED = "terminated"


class Transcoder:
    """Manages one ffmpeg subprocess that re-segments an input HLS source.

    See module docstring for the full state-machine contract.
    """

    def __init__(
        self,
        input_url: str,
        output_dir: Path,
        accel: AccelProfile,
        warming_timeout: float = 8.0,
        stall_timeout: float = 8.0,
        hls_segment_seconds: int = 2,
        _poll_interval: float = 0.25,
    ) -> None:
        self._input_url = input_url
        self._output_dir = Path(output_dir)
        self._accel = accel
        self._warming_timeout = warming_timeout
        self._stall_timeout = stall_timeout
        self._hls_segment_seconds = hls_segment_seconds
        self._poll_interval = _poll_interval

        self._state: TranscoderState = TranscoderState.IDLE
        self._idle_reason: Optional[str] = None
        self._exit_code: Optional[int] = None

    @property
    def state(self) -> TranscoderState:
        return self._state

    @property
    def idle_reason(self) -> Optional[str]:
        return self._idle_reason

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    @property
    def master_playlist(self) -> Path:
        return self._output_dir / "master.m3u8"

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    def _build_argv(self) -> list[str]:
        """Construct the ffmpeg argv list from input_url + accel + config."""
        argv: list[str] = [
            self._accel.ffmpeg_path,
            "-hide_banner",
            "-loglevel", "info",
            "-nostdin",
        ]
        if self._accel.decoder and self._accel.decoder != "none":
            argv += ["-hwaccel", self._accel.decoder]
        argv += [
            "-fflags", "+genpts",
            "-i", self._input_url,
            "-vf", "null",
            "-c:v", self._accel.encoder,
        ]
        argv += _ENCODER_FLAGS.get(self._accel.encoder, [])
        argv += [
            "-c:a", "copy",
            "-f", "hls",
            "-hls_time", str(self._hls_segment_seconds),
            "-hls_list_size", "6",
            "-hls_flags", "delete_segments+append_list+independent_segments",
            "-hls_segment_filename", str(self._output_dir / "seg_%05d.ts"),
            str(self._output_dir / "variant.m3u8"),
        ]
        return argv

    def _write_master_playlist(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        (self._output_dir / "master.m3u8").write_text(
            _MASTER_PLAYLIST, encoding="utf-8"
        )

    def start(self) -> None:
        if self._state != TranscoderState.IDLE:
            raise RuntimeError(
                f"start() called in state {self._state.name}; expected IDLE"
            )
        self._write_master_playlist()
        # Full Popen + thread spawn lands in Task 5. For now just transition
        # to SPAWNING so the test in Task 4 can validate the playlist.
        self._state = TranscoderState.SPAWNING

    def stop(self, drain_seconds: float = 2.0) -> None:
        # Full lifecycle teardown lands in later tasks. Minimal no-op for now
        # so test setUps that call stop() in finally blocks don't crash.
        self._state = TranscoderState.TERMINATED


# Per-encoder flags. Values verified against current ffmpeg HLS muxer +
# encoder docs at start of P2.2 implementation session (per CLAUDE.md rule
# #2). HW-encoder paths are untested-pending-hardware per P2.1 B2.
_ENCODER_FLAGS = {
    "libx264": [
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-profile:v", "main",
        "-level", "4.0",
        "-pix_fmt", "yuv420p",
    ],
    "h264_nvenc": [
        "-preset", "p5",
        "-tune", "ll",
        "-profile:v", "main",
        "-rc", "cbr",
        "-b:v", "3M",
    ],
    "h264_qsv": [
        "-preset", "veryfast",
        "-profile:v", "main",
        "-b:v", "3M",
    ],
    "h264_amf": [
        "-quality", "balanced",
        "-profile:v", "main",
        "-b:v", "3M",
    ],
}

_MASTER_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,"
    'CODECS="avc1.4d401f,mp4a.40.2"\n'
    "variant.m3u8\n"
)
