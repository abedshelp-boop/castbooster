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
import os
import re
import subprocess
import sys
import threading
import time
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
        self._state_lock = threading.Lock()
        self._ready_event = threading.Event()
        self._process: Optional[subprocess.Popen] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._poller_thread: Optional[threading.Thread] = None
        self._stop_requested = threading.Event()
        self._warming_started_monotonic: float = 0.0
        self._last_seg_count: int = 0
        self._last_new_seg_monotonic: float = 0.0

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

    def _set_state_locked(
        self,
        new_state: TranscoderState,
        idle_reason: Optional[str] = None,
    ) -> None:
        """Caller MUST hold self._state_lock."""
        if self._state == new_state:
            return
        log.info(
            "transcoder state: %s -> %s%s",
            self._state.name, new_state.name,
            f" ({idle_reason})" if idle_reason else "",
        )
        self._state = new_state
        if idle_reason is not None and self._idle_reason is None:
            self._idle_reason = idle_reason
        if new_state in (TranscoderState.READY, TranscoderState.FAILED):
            self._ready_event.set()

    def _write_master_playlist(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        (self._output_dir / "master.m3u8").write_text(
            _MASTER_PLAYLIST, encoding="utf-8"
        )

    def start(self) -> None:
        with self._state_lock:
            if self._state != TranscoderState.IDLE:
                raise RuntimeError(
                    f"start() called in state {self._state.name}; expected IDLE"
                )
            self._set_state_locked(TranscoderState.SPAWNING)

        self._write_master_playlist()

        argv = self._build_argv()
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        log.info("transcoder spawning: %s", " ".join(argv[:6]) + " ...")
        self._process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._warming_started_monotonic = time.monotonic()

        with self._state_lock:
            self._set_state_locked(TranscoderState.WARMING)

        # Threads land in Tasks 7 + 8. For now spawn the poller thread as a
        # bare loop so the state visibly remains WARMING in tests.
        self._poller_thread = threading.Thread(
            target=self._watchdog_poller_loop,
            name=f"transcoder-poller-{id(self):x}",
            daemon=True,
        )
        self._poller_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader_loop,
            name=f"transcoder-stderr-{id(self):x}",
            daemon=True,
        )
        self._stderr_thread.start()

    def stop(self, drain_seconds: float = 2.0) -> None:
        # Full lifecycle teardown lands in later tasks. Minimal no-op for now
        # so test setUps that call stop() in finally blocks don't crash.
        self._stop_requested.set()
        with self._state_lock:
            self._set_state_locked(TranscoderState.TERMINATED)

    def _is_ready_on_disk(self) -> tuple[bool, int]:
        """Returns (ready, seg_count). Ready iff >= 2 segs AND master AND variant exist."""
        try:
            segs = sorted(self._output_dir.glob("seg_*.ts"))
        except OSError:
            return False, 0
        seg_count = len(segs)
        if seg_count < 2:
            return False, seg_count
        if not (self._output_dir / "master.m3u8").exists():
            return False, seg_count
        if not (self._output_dir / "variant.m3u8").exists():
            return False, seg_count
        return True, seg_count

    def _check_process_exit_locked(self) -> bool:
        """Returns True iff state transitioned to FAILED due to subprocess exit.
        Caller must NOT hold _state_lock — this method acquires it briefly.
        """
        assert self._process is not None
        code = self._process.poll()
        if code is None:
            return False
        with self._state_lock:
            self._exit_code = code
            if self._state in (
                TranscoderState.SPAWNING,
                TranscoderState.WARMING,
            ):
                self._set_state_locked(
                    TranscoderState.FAILED,
                    idle_reason="subprocess_died_early",
                )
                return True
            if self._state in (
                TranscoderState.READY,
                TranscoderState.STREAMING,
                TranscoderState.STALLED,
            ) and code != 0:
                self._set_state_locked(
                    TranscoderState.FAILED, idle_reason="unknown"
                )
                return True
            # state already FAILED/TERMINATING/TERMINATED, or exited 0 post-READY
            return False

    def _watchdog_poller_loop(self) -> None:
        """Drives WARMING -> READY -> STREAMING <-> STALLED + early-exit /
        timeout failures. Exits when state is FAILED or TERMINATED.

        Later tasks (11, 12) extend this loop. Keep it readable.
        """
        while not self._stop_requested.is_set():
            time.sleep(self._poll_interval)
            with self._state_lock:
                current = self._state
            if current in (
                TranscoderState.FAILED,
                TranscoderState.TERMINATING,
                TranscoderState.TERMINATED,
            ):
                return

            # Check subprocess health FIRST so an exited proc can't be reported
            # as READY just because someone touched files at the right moment.
            if self._check_process_exit_locked():
                return

            if current == TranscoderState.WARMING:
                # Check timeout BEFORE readiness so a slow upstream that takes
                # >warming_timeout to produce 2 segs trips FAILED, not READY.
                if (
                    time.monotonic() - self._warming_started_monotonic
                    > self._warming_timeout
                ):
                    with self._state_lock:
                        if self._state == TranscoderState.WARMING:
                            self._set_state_locked(
                                TranscoderState.FAILED,
                                idle_reason="warming_timed_out",
                            )
                    continue
                ready, seg_count = self._is_ready_on_disk()
                if ready:
                    with self._state_lock:
                        if self._state == TranscoderState.WARMING:
                            self._set_state_locked(TranscoderState.READY)
                            self._last_seg_count = seg_count
                            self._last_new_seg_monotonic = time.monotonic()
                    continue

            if current == TranscoderState.READY:
                _, seg_count = self._is_ready_on_disk()
                if seg_count > self._last_seg_count:
                    with self._state_lock:
                        if self._state == TranscoderState.READY:
                            self._set_state_locked(TranscoderState.STREAMING)
                            self._last_seg_count = seg_count
                            self._last_new_seg_monotonic = time.monotonic()
                    continue

            if current == TranscoderState.STREAMING:
                _, seg_count = self._is_ready_on_disk()
                if seg_count > self._last_seg_count:
                    self._last_seg_count = seg_count
                    self._last_new_seg_monotonic = time.monotonic()
                elif (
                    time.monotonic() - self._last_new_seg_monotonic
                    > self._stall_timeout
                ):
                    with self._state_lock:
                        if self._state == TranscoderState.STREAMING:
                            self._set_state_locked(TranscoderState.STALLED)
                continue

            if current == TranscoderState.STALLED:
                _, seg_count = self._is_ready_on_disk()
                if seg_count > self._last_seg_count:
                    self._last_seg_count = seg_count
                    self._last_new_seg_monotonic = time.monotonic()
                    with self._state_lock:
                        if self._state == TranscoderState.STALLED:
                            self._set_state_locked(TranscoderState.STREAMING)
                continue

    def _stderr_reader_loop(self) -> None:
        """Drives state -> FAILED on any line matching _FATAL_PATTERNS.

        Continues reading after the first fatal match so subsequent stderr
        is still logged (useful for diagnostics). Exits when stderr closes.
        """
        assert self._process is not None
        try:
            for raw_line in self._process.stderr:  # type: ignore[union-attr]
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    log.debug("ffmpeg stderr: %s", line)
                reason = _classify_stderr_line(line)
                if reason is not None:
                    with self._state_lock:
                        if self._state in (
                            TranscoderState.SPAWNING,
                            TranscoderState.WARMING,
                            TranscoderState.READY,
                            TranscoderState.STREAMING,
                            TranscoderState.STALLED,
                        ):
                            self._set_state_locked(
                                TranscoderState.FAILED, idle_reason=reason
                            )
        except Exception:
            log.exception("stderr reader crashed")


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

# Fatal-pattern table. Each entry is (compiled regex, idle_reason token).
# Patterns are case-insensitive. Order matters only for documentation;
# all patterns are tried until one matches.
_FATAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"No NVENC capable devices found", re.I), "hwaccel_unavailable"),
    (re.compile(r"nvenc.*not supported", re.I), "hwaccel_unavailable"),
    (re.compile(r"Cannot load (libcuda|nvcuda)", re.I), "hwaccel_unavailable"),
    (re.compile(r"qsv.*not (supported|available)", re.I), "hwaccel_unavailable"),
    (re.compile(r"Connection refused", re.I), "input_unreachable"),
    (re.compile(r"404 Not Found", re.I), "input_unreachable"),
    (re.compile(r"HTTP error [45]\d\d", re.I), "input_unreachable"),
    (re.compile(r"Invalid data found when processing input", re.I), "input_unreachable"),
    (re.compile(r"Error.*opening encoder", re.I), "encoder_init_failed"),
    (re.compile(r"Cannot initialize.*encoder", re.I), "encoder_init_failed"),
    (re.compile(r"Failed to open codec", re.I), "encoder_init_failed"),
]


def _classify_stderr_line(line: str) -> Optional[str]:
    """Return an idle_reason token if `line` matches a fatal pattern, else None.

    Called from the stderr-reader thread for every line ffmpeg emits.
    Non-None returns trigger the WARMING/READY/STREAMING/STALLED -> FAILED
    transition.
    """
    if not line:
        return None
    for pattern, reason in _FATAL_PATTERNS:
        if pattern.search(line):
            return reason
    return None
