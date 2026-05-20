"""Per-session ffmpeg subprocess manager with explicit state machine.

State machine (every observable state is enumerated; no predicate ever
treats "not active" as "done" — see 2026-04-22 IDLE-race lesson):

    IDLE         constructed; start() not yet called
    SPAWNING     _current slot in SPAWNING
    WARMING      _current slot in WARMING
    READY        _current slot in READY                            (Chromecast can fetch)
    STREAMING    _current slot in STREAMING                        (Chromecast can fetch)
    STALLED      _current slot in STALLED                          (Chromecast can fetch)
    RELOADING    _current STREAMING/STALLED/READY + _next WARMING  (Chromecast can fetch from _current)
    FAILED       _current FAILED + _next is None or also FAILED
    TERMINATING  stop() called; draining both slots
    TERMINATED   all slots reaped + output dirs deleted

Threading: each _ProcessSlot owns one stderr-reader thread + one
watchdog-poller thread. Transcoder owns the state lock and the slot refs.
State mutations go through _set_state_locked() under _state_lock.

P2.3 added set_filter_chain(chain) -> bool for hot-reload via
spawn-new-then-kill-old. RELOADING covers the window where NEW is
WARMING in <base>/v<N+1>/ while OLD continues serving from <base>/v<N>/.
"""
from __future__ import annotations

import logging
import math
import re
import shutil
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from castbooster.ffmpeg_probe import AccelProfile
from castbooster.filter_chain import FilterChain, NoopFilter
from castbooster.pipeline_spec import PipelineSpec

log = logging.getLogger(__name__)


class _SlotState(Enum):
    """Per-process sub-state. Module-private; not part of public API."""
    SPAWNING = "spawning"
    WARMING = "warming"
    READY = "ready"
    STREAMING = "streaming"
    STALLED = "stalled"
    FAILED = "failed"
    TERMINATING = "terminating"
    TERMINATED = "terminated"


class TranscoderState(Enum):
    IDLE = "idle"
    SPAWNING = "spawning"
    WARMING = "warming"
    READY = "ready"
    STREAMING = "streaming"
    STALLED = "stalled"
    RELOADING = "reloading"
    FAILED = "failed"
    TERMINATING = "terminating"
    TERMINATED = "terminated"


# P3.3 — Multi-proc helpers
# -------------------------
# Base video bitrate for the sqrt-scaled multi-proc encode path.
# Matches the HW-encoder `-b:v 3M` value in _ENCODER_FLAGS (libx264 has no
# explicit -b:v in single-proc mode; it relies on -preset). The multi-proc
# path always sets -b:v because rife output is fed as rawvideo so the
# encoder has no original-rate signal to defer to.
_BASE_BITRATE_BPS = 3_000_000


def _scaled_bitrate(base_bps: int, src_fps: float, tgt_fps: int) -> int:
    """sqrt scaling per parent P3 spec D6: new = base * sqrt(tgt/src).

    Examples (base=3_000_000):
        24 → 60: ratio sqrt(60/24) ≈ 1.581 → ~4.74 Mbps
        24 → 30: ratio sqrt(30/24) ≈ 1.118 → ~3.35 Mbps

    Guards against div-by-zero by clamping src to >= 1.0.
    """
    return int(base_bps * math.sqrt(tgt_fps / max(src_fps, 1.0)))


def _parse_encoder_input_format(spec: str) -> tuple[str, str]:
    """``"rawvideo:yuv420p:1920x1080"`` → ``("yuv420p", "1920x1080")``.

    Only the ``rawvideo:<pix_fmt>:<WxH>`` form is supported in P3.3.
    Raises ``ValueError`` on any other shape.
    """
    parts = spec.split(":")
    if len(parts) != 3 or parts[0] != "rawvideo":
        raise ValueError(f"unsupported encoder_input_format: {spec!r}")
    return parts[1], parts[2]


def _spec_from_chain(chain: FilterChain) -> Optional[PipelineSpec]:
    """Return the first non-None ``pipeline_spec()`` from the chain, or None.

    Raises ``ValueError`` if more than one stage in the chain returns a
    non-None PipelineSpec — P3.3 is YAGNI on multi-PipelineSpec chains;
    no real filter needs that yet (P4 Anime4K is a -vf filter, not a
    pipeline filter).
    """
    spec: Optional[PipelineSpec] = None
    for stage in chain.stages:
        s = stage.pipeline_spec()
        if s is None:
            continue
        if spec is not None:
            raise ValueError(
                "FilterChain has more than one stage returning a "
                "PipelineSpec — not supported in P3.3"
            )
        spec = s
    return spec


def _build_argv(
    input_url: str,
    output_dir: Path,
    accel: AccelProfile,
    hls_segment_seconds: int,
    vf_fragment: str = "null",                 # P2.3 Task 3 wires in filter_chain.render()
    pipeline_spec: Optional[PipelineSpec] = None,   # P3.3 multi-proc path
    src_fps: Optional[float] = None,                # P3.3 sqrt-bitrate scaling input
) -> list[str]:
    if pipeline_spec is None:
        # Single-Popen path — UNCHANGED from P2.5/P3.1
        argv: list[str] = [
            accel.ffmpeg_path,
            "-hide_banner",
            "-loglevel", "info",
            "-nostdin",
        ]
        if accel.decoder and accel.decoder != "none":
            argv += ["-hwaccel", accel.decoder]
        argv += [
            "-fflags", "+genpts",
            "-i", input_url,
            "-vf", vf_fragment,
            "-c:v", accel.encoder,
        ]
        argv += _ENCODER_FLAGS.get(accel.encoder, [])
        argv += [
            "-force_key_frames",
            f"expr:gte(t,n_forced*{hls_segment_seconds})",
        ]
        argv += [
            # 2026-05-19 P2.5: always re-encode audio to AAC. Fixes AC3/EAC3
            # silent-playback on Chromecast 3rd gen (which only supports AAC
            # / MP3). Generation loss on AAC->AAC is imperceptible at 192k;
            # CPU cost is ~1-2% on SW transcoding budgets.
            "-c:a", "aac",
            "-b:a", "192k",
            "-f", "hls",
            "-hls_time", str(hls_segment_seconds),
            # 2026-05-18: PLAYLIST-TYPE:VOD enables Chromecast drag-seek on the
            # output playlist. Without it the player treats the live-style
            # playlist as un-seekable and disables the scrubber UI. EXT-X-ENDLIST
            # gets written on clean ffmpeg exit (session stop or upstream EOF).
            "-hls_playlist_type", "vod",
            # 2026-05-18: 6-segment sliding window with delete_segments caused
            # 404s on /output/seg_NNNNN.ts when SW transcoding ran at 22x
            # realtime — ffmpeg deleted segments faster than the Chromecast
            # could fetch them. Keep all segments listed + on disk for the
            # duration of the session. output_dir is wiped on stop(), so disk
            # use is bounded by the session length (~200KB per 2s segment).
            "-hls_list_size", "0",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", str(output_dir / "seg_%05d.ts"),
            str(output_dir / "variant.m3u8"),
        ]
        return argv

    # P3.3 multi-proc path — encode ffmpeg reads rawvideo from stdin.
    # The decode + side processes live inside the side_task_factory
    # (RIFEFilter spawns its own decode). This branch only describes the
    # PRIMARY encode ffmpeg's argv.
    pix_fmt, w_h = _parse_encoder_input_format(pipeline_spec.encoder_input_format)
    # src_fps may legitimately be unknown (probe failed). Default to
    # target_fps so the sqrt ratio is 1.0 (no bitrate scaling).
    effective_src = src_fps if src_fps is not None else float(pipeline_spec.target_fps)
    argv = [
        accel.ffmpeg_path,
        "-hide_banner",
        "-loglevel", "info",
        "-nostdin",
        # Rawvideo input from stdin. Dims + pix_fmt + framerate are
        # mandatory because rawvideo has no header (per ffmpeg-all
        # docs §20.21).
        "-f", "rawvideo",
        "-pix_fmt", pix_fmt,
        "-s", w_h,
        "-r", str(pipeline_spec.target_fps),
        "-i", "-",
        # vf_fragment is typically "null" — filtering happened upstream
        # in the side task (rife output). Keep the slot honoring the
        # chain's render() for forward compatibility (P4 may chain
        # rife + a -vf shader).
        "-vf", vf_fragment,
        "-c:v", accel.encoder,
    ]
    argv += _ENCODER_FLAGS.get(accel.encoder, [])
    argv += [
        # Output framerate matches the target (encoder retunes).
        "-r", str(pipeline_spec.target_fps),
        # P3.3 D6: sqrt-scaled bitrate. Note: this OVERRIDES any -b:v
        # in _ENCODER_FLAGS (libx264 has none; the HW encoders' 3M
        # default is replaced by the scaled value here).
        "-b:v", str(_scaled_bitrate(_BASE_BITRATE_BPS, effective_src, pipeline_spec.target_fps)),
        "-force_key_frames",
        f"expr:gte(t,n_forced*{hls_segment_seconds})",
        # Always-AAC audio (P2.5). Multi-proc encoder reads rawvideo
        # from stdin which carries no audio; -c:a aac with no audio
        # input is a no-op (ffmpeg drops the empty audio track). Audio
        # source for multi-proc is P3.4/proxy's responsibility (out of
        # scope for P3.3 — see spec §3.4 "Note on audio").
        "-c:a", "aac",
        "-b:a", "192k",
        "-f", "hls",
        "-hls_time", str(hls_segment_seconds),
        "-hls_playlist_type", "vod",
        "-hls_list_size", "0",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", str(output_dir / "seg_%05d.ts"),
        str(output_dir / "variant.m3u8"),
    ]
    return argv


class _ProcessSlot:
    """One ffmpeg subprocess + its threads + its output_dir + its sub-state.

    Drives its own sub_state via internal threads; surfaces changes via the
    on_sub_state_change callback so Transcoder can update aggregate state.
    """

    def __init__(
        self,
        output_dir: Path,
        warming_timeout: float,
        stall_timeout: float,
        poll_interval: float,
        on_sub_state_change: Callable[["_ProcessSlot", "_SlotState"], None],
    ) -> None:
        self._output_dir = Path(output_dir)
        self._warming_timeout = warming_timeout
        self._stall_timeout = stall_timeout
        self._poll_interval = poll_interval
        self._on_sub_state_change = on_sub_state_change

        self._sub_state: _SlotState = _SlotState.SPAWNING
        self._idle_reason: Optional[str] = None
        self._exit_code: Optional[int] = None
        self._sub_state_lock = threading.Lock()
        self._ready_event = threading.Event()
        self._process: Optional[subprocess.Popen] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._poller_thread: Optional[threading.Thread] = None
        self._stop_requested = threading.Event()
        self._warming_started_monotonic: float = 0.0
        self._last_seg_count: int = 0
        self._last_new_seg_monotonic: float = 0.0

    # ---- properties ----
    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def sub_state(self) -> _SlotState:
        return self._sub_state

    @property
    def idle_reason(self) -> Optional[str]:
        return self._idle_reason

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    @property
    def ready_event(self) -> threading.Event:
        return self._ready_event

    # ---- lifecycle ----
    def _write_master_playlist(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        (self._output_dir / "master.m3u8").write_text(_MASTER_PLAYLIST, encoding="utf-8")

    def start(self, argv: list[str]) -> None:
        # Guard: if stop() was called before start() (race during RELOADING teardown),
        # skip directory creation and process spawn entirely.  The slot is already
        # TERMINATING/TERMINATED and its output_dir was (or will be) cleaned up by stop().
        with self._sub_state_lock:
            if self._sub_state in (_SlotState.TERMINATING, _SlotState.TERMINATED):
                return
        self._write_master_playlist()
        # Second guard after mkdir: stop() may have completed _cleanup_output_dir()
        # between the check above and the mkdir.  If so, remove the freshly-created
        # directory ourselves so stop()'s cleanup is not undone.
        if self._stop_requested.is_set():
            self._cleanup_output_dir()
            return
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        log.info("slot spawning in %s", self._output_dir)
        # 2026-05-17: log argv so diagnostic runs can reproduce the exact
        # command line by hand. The /upstream/* loopback URL leaks the
        # session token but the log is local-only.
        log.info("ffmpeg argv: %s", " ".join(repr(a) for a in argv))
        self._process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        log.info("ffmpeg subprocess PID=%s started", getattr(self._process, "pid", "?"))
        self._warming_started_monotonic = time.monotonic()
        with self._sub_state_lock:
            self._set_sub_state_locked(_SlotState.WARMING)
        self._poller_thread = threading.Thread(
            target=self._watchdog_poller_loop,
            name=f"slot-poller-{id(self):x}",
            daemon=True,
        )
        self._poller_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader_loop,
            name=f"slot-stderr-{id(self):x}",
            daemon=True,
        )
        self._stderr_thread.start()

    def wait_until_ready(self, timeout: float) -> bool:
        signalled = self._ready_event.wait(timeout=timeout)
        if not signalled:
            return False
        return self._sub_state in (
            _SlotState.READY,
            _SlotState.STREAMING,
            _SlotState.STALLED,
        )

    def stop(self, drain_seconds: float = 2.0) -> None:
        with self._sub_state_lock:
            if self._sub_state in (_SlotState.TERMINATING, _SlotState.TERMINATED):
                return
            self._set_sub_state_locked(_SlotState.TERMINATING)
        self._stop_requested.set()
        self._ready_event.set()        # unblock any wait_until_ready() waiters promptly
        if self._process is not None:
            try:
                if self._process.stdin is not None:
                    self._process.stdin.write(b"q\n")
                    self._process.stdin.flush()
            except Exception:
                log.exception("stdin q-shutdown failed")
            try:
                self._exit_code = self._process.wait(timeout=drain_seconds)
            except subprocess.TimeoutExpired:
                log.warning("graceful drain timed out — escalating to kill")
                try:
                    self._process.kill()
                    self._exit_code = self._process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    log.error("ffmpeg refused to die even after kill()")
                except Exception:
                    log.exception("hard kill failed")
        for th in (self._stderr_thread, self._poller_thread):
            if th is not None and th.is_alive():
                th.join(timeout=1.0)
        self._cleanup_output_dir()
        with self._sub_state_lock:
            self._set_sub_state_locked(_SlotState.TERMINATED)

    # ---- helpers (moved from Transcoder; behavior unchanged) ----
    def _set_sub_state_locked(self, new: _SlotState, idle_reason: Optional[str] = None) -> None:
        """Caller MUST hold self._sub_state_lock."""
        if self._sub_state == new:
            return
        log.info(
            "slot sub_state: %s -> %s%s",
            self._sub_state.name, new.name,
            f" ({idle_reason})" if idle_reason else "",
        )
        self._sub_state = new
        if idle_reason is not None and self._idle_reason is None:
            self._idle_reason = idle_reason
        if new in (_SlotState.READY, _SlotState.FAILED):
            self._ready_event.set()
        # Fire callback OUTSIDE the lock to avoid Transcoder→slot lock-order issues
        cb = self._on_sub_state_change
        self._sub_state_lock.release()
        try:
            cb(self, new)
        finally:
            self._sub_state_lock.acquire()

    def _cleanup_output_dir(self) -> None:
        try:
            shutil.rmtree(self._output_dir, ignore_errors=False)
            return
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("output_dir rmtree failed (will retry): %s", e)
        time.sleep(0.5)
        try:
            shutil.rmtree(self._output_dir, ignore_errors=True)
        except Exception:
            log.exception("output_dir rmtree retry failed; giving up")

    def _is_ready_on_disk(self) -> tuple[bool, int]:
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
        """Returns True iff slot transitioned to FAILED. Acquires sub_state_lock briefly."""
        assert self._process is not None
        code = self._process.poll()
        if code is None:
            return False
        final_ready = False
        final_seg_count = 0
        if code == 0:
            final_ready, final_seg_count = self._is_ready_on_disk()
        with self._sub_state_lock:
            self._exit_code = code
            if self._sub_state in (_SlotState.SPAWNING, _SlotState.WARMING):
                if final_ready:
                    self._set_sub_state_locked(_SlotState.READY)
                    self._last_seg_count = final_seg_count
                    self._last_new_seg_monotonic = time.monotonic()
                    return False
                self._set_sub_state_locked(_SlotState.FAILED, idle_reason="subprocess_died_early")
                return True
            if self._sub_state in (
                _SlotState.READY, _SlotState.STREAMING, _SlotState.STALLED,
            ) and code != 0:
                self._set_sub_state_locked(_SlotState.FAILED, idle_reason="unknown")
                return True
            return False

    def _watchdog_poller_loop(self) -> None:
        while not self._stop_requested.is_set():
            time.sleep(self._poll_interval)
            with self._sub_state_lock:
                current = self._sub_state
            if current in (
                _SlotState.FAILED, _SlotState.TERMINATING, _SlotState.TERMINATED,
            ):
                return
            if self._check_process_exit_locked():
                return
            if current == _SlotState.WARMING:
                if (
                    time.monotonic() - self._warming_started_monotonic
                    > self._warming_timeout
                ):
                    with self._sub_state_lock:
                        if self._sub_state == _SlotState.WARMING:
                            self._set_sub_state_locked(
                                _SlotState.FAILED, idle_reason="warming_timed_out",
                            )
                    continue
                ready, seg_count = self._is_ready_on_disk()
                if ready:
                    with self._sub_state_lock:
                        if self._sub_state == _SlotState.WARMING:
                            self._set_sub_state_locked(_SlotState.READY)
                            self._last_seg_count = seg_count
                            self._last_new_seg_monotonic = time.monotonic()
                    continue
            if current == _SlotState.READY:
                _, seg_count = self._is_ready_on_disk()
                if seg_count > self._last_seg_count:
                    with self._sub_state_lock:
                        if self._sub_state == _SlotState.READY:
                            self._set_sub_state_locked(_SlotState.STREAMING)
                            self._last_seg_count = seg_count
                            self._last_new_seg_monotonic = time.monotonic()
                continue
            if current == _SlotState.STREAMING:
                _, seg_count = self._is_ready_on_disk()
                if seg_count > self._last_seg_count:
                    self._last_seg_count = seg_count
                    self._last_new_seg_monotonic = time.monotonic()
                elif (
                    time.monotonic() - self._last_new_seg_monotonic
                    > self._stall_timeout
                ):
                    with self._sub_state_lock:
                        if self._sub_state == _SlotState.STREAMING:
                            self._set_sub_state_locked(_SlotState.STALLED)
                continue
            if current == _SlotState.STALLED:
                _, seg_count = self._is_ready_on_disk()
                if seg_count > self._last_seg_count:
                    self._last_seg_count = seg_count
                    self._last_new_seg_monotonic = time.monotonic()
                    with self._sub_state_lock:
                        if self._sub_state == _SlotState.STALLED:
                            self._set_sub_state_locked(_SlotState.STREAMING)
                continue

    def _stderr_reader_loop(self) -> None:
        assert self._process is not None
        try:
            for raw_line in self._process.stderr:  # type: ignore[union-attr]
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    log.debug("ffmpeg stderr: %s", line)
                reason = _classify_stderr_line(line)
                if reason is not None:
                    # Surface the offending line at WARNING so the reason
                    # behind a FAILED transition is visible without needing
                    # CASTBOOSTER_LOG_LEVEL=DEBUG. Closes the 2026-05-17
                    # 'input_unreachable with no captured stderr' gap.
                    log.warning("ffmpeg fatal stderr (reason=%s): %s", reason, line)
                    with self._sub_state_lock:
                        if self._sub_state in (
                            _SlotState.SPAWNING, _SlotState.WARMING,
                            _SlotState.READY, _SlotState.STREAMING, _SlotState.STALLED,
                        ):
                            self._set_sub_state_locked(_SlotState.FAILED, idle_reason=reason)
        except Exception:
            log.exception("stderr reader crashed")


_SUBSTATE_TO_STATE: dict[_SlotState, TranscoderState] = {
    _SlotState.SPAWNING:    TranscoderState.SPAWNING,
    _SlotState.WARMING:     TranscoderState.WARMING,
    _SlotState.READY:       TranscoderState.READY,
    _SlotState.STREAMING:   TranscoderState.STREAMING,
    _SlotState.STALLED:     TranscoderState.STALLED,
    _SlotState.FAILED:      TranscoderState.FAILED,
    _SlotState.TERMINATING: TranscoderState.TERMINATING,
    _SlotState.TERMINATED:  TranscoderState.TERMINATED,
}


class Transcoder:
    """Manages one ffmpeg subprocess per cast session that re-segments an input HLS source.

    State machine (every observable state is enumerated; no predicate ever
    treats "not active" as "done" — see 2026-04-22 IDLE-race lesson):

        IDLE         constructed; start() not yet called
        SPAWNING     _current slot in SPAWNING
        WARMING      _current slot in WARMING
        READY        _current slot in READY
        STREAMING    _current slot in STREAMING
        STALLED      _current slot in STALLED
        FAILED       _current slot in FAILED AND _next is None or also FAILED
        TERMINATING  stop() called; draining
        TERMINATED   subprocess reaped + output_dir deleted

    P2.3 adds RELOADING — wired in Task 5.
    """

    def __init__(
        self,
        input_url: str,
        output_dir: Optional[Path] = None,
        accel: Optional[AccelProfile] = None,          # required; type-checked below
        *,
        base_output_dir: Optional[Path] = None,
        filter_chain: Optional[FilterChain] = None,
        warming_timeout: float = 8.0,
        stall_timeout: float = 8.0,
        hls_segment_seconds: int = 2,
        _poll_interval: float = 0.25,
    ) -> None:
        if accel is None:
            raise TypeError("Transcoder requires accel: AccelProfile")
        base = base_output_dir if base_output_dir is not None else output_dir
        if base is None:
            raise TypeError("Transcoder requires base_output_dir (or legacy output_dir)")
        self._input_url = input_url
        self._accel = accel
        self._base_output_dir = Path(base)
        self._filter_chain: FilterChain = filter_chain or FilterChain([NoopFilter()])
        self._warming_timeout = warming_timeout
        self._stall_timeout = stall_timeout
        self._hls_segment_seconds = hls_segment_seconds
        self._poll_interval = _poll_interval

        self._state: TranscoderState = TranscoderState.IDLE
        self._state_lock = threading.Lock()
        self._ready_event = threading.Event()
        self._current: Optional[_ProcessSlot] = None
        self._next: Optional[_ProcessSlot] = None      # Task 6 populates this
        self._slot_counter: int = 0                    # Task 6 increments this
        self._last_reload_error: Optional[str] = None
        self._filter_chain_pending: Optional[FilterChain] = None

    # ---- public properties ----
    @property
    def state(self) -> TranscoderState:
        return self._state

    @property
    def idle_reason(self) -> Optional[str]:
        if self._current is None:
            return None
        return self._current.idle_reason

    @property
    def exit_code(self) -> Optional[int]:
        if self._current is None:
            return None
        return self._current.exit_code

    @property
    def output_dir(self) -> Path:
        """The CURRENT slot's output_dir. Before start() this is a placeholder."""
        if self._current is None:
            # Pre-start: synthesize the v1 path so callers querying early get something predictable.
            return self._base_output_dir / "v1"
        return self._current.output_dir

    @property
    def master_playlist(self) -> Path:
        return self.output_dir / "master.m3u8"

    @property
    def last_reload_error(self) -> Optional[str]:
        """The idle_reason from the most recent failed set_filter_chain() call.

        Cleared on the next successful reload. Survives across multiple failed
        reloads — only the most recent reason is exposed.
        """
        return self._last_reload_error

    # ---- lifecycle ----
    def start(self) -> None:
        with self._state_lock:
            if self._state != TranscoderState.IDLE:
                raise RuntimeError(
                    f"start() called in state {self._state.name}; expected IDLE"
                )
            self._set_state_locked(TranscoderState.SPAWNING)
            self._slot_counter = 1
        slot_dir = self._base_output_dir / f"v{self._slot_counter}"
        self._current = _ProcessSlot(
            output_dir=slot_dir,
            warming_timeout=self._warming_timeout,
            stall_timeout=self._stall_timeout,
            poll_interval=self._poll_interval,
            on_sub_state_change=self._on_current_substate_change,
        )
        argv = _build_argv(
            input_url=self._input_url,
            output_dir=slot_dir,
            accel=self._accel,
            hls_segment_seconds=self._hls_segment_seconds,
            vf_fragment=self._filter_chain.render(self._input_url),
        )
        self._current.start(argv)
        # _current's start() already moved sub_state to WARMING; mirror to aggregate
        with self._state_lock:
            self._set_state_locked(TranscoderState.WARMING)

    def wait_until_ready(self, timeout: Optional[float] = None) -> bool:
        if timeout is None:
            timeout = self._warming_timeout
        signalled = self._ready_event.wait(timeout=timeout)
        if not signalled:
            return False
        return self._state in (
            TranscoderState.READY, TranscoderState.STREAMING, TranscoderState.STALLED,
        )

    def stop(self, drain_seconds: float = 2.0) -> None:
        with self._state_lock:
            if self._state in (TranscoderState.TERMINATING, TranscoderState.TERMINATED):
                return
            self._set_state_locked(TranscoderState.TERMINATING)
            # Snapshot refs under the lock to avoid a data race with set_filter_chain
            # writing self._next outside the lock during its wait_until_ready() call.
            next_slot = self._next
            current_slot = self._current
            self._next = None
        # Stop NEW first if it exists (Task 6+); always stop _current
        if next_slot is not None:
            next_slot.stop(drain_seconds=drain_seconds)
        if current_slot is not None:
            current_slot.stop(drain_seconds=drain_seconds)
        with self._state_lock:
            self._set_state_locked(TranscoderState.TERMINATED)

    # ---- back-compat shim for tests that call t._build_argv() ----
    def _build_argv(self) -> list[str]:
        return _build_argv(
            input_url=self._input_url,
            output_dir=self._base_output_dir / "v1",
            accel=self._accel,
            hls_segment_seconds=self._hls_segment_seconds,
            vf_fragment=self._filter_chain.render(self._input_url),
        )

    # ---- private internals ----
    def _set_state_locked(self, new: TranscoderState, idle_reason: Optional[str] = None) -> None:
        """Caller MUST hold self._state_lock."""
        if self._state == new:
            return
        log.info(
            "transcoder state: %s -> %s%s",
            self._state.name, new.name,
            f" ({idle_reason})" if idle_reason else "",
        )
        self._state = new
        if new in (TranscoderState.READY, TranscoderState.FAILED):
            self._ready_event.set()

    def _on_current_substate_change(
        self, slot: _ProcessSlot, new_sub_state: _SlotState
    ) -> None:
        """Callback fired by _current when its sub_state changes.
        Translates sub_state to public TranscoderState.
        """
        with self._state_lock:
            # Ignore callbacks if we're already TERMINATING/TERMINATED
            if self._state in (TranscoderState.TERMINATING, TranscoderState.TERMINATED):
                return
            # Ignore callbacks from slots that are no longer _current (e.g. OLD
            # slot being stopped after a successful promotion).
            if slot is not self._current:
                return
            if self._next is not None:
                # We're RELOADING. The public state aggregate stays RELOADING
                # regardless of _current's sub-state changes. _current may even
                # transition to FAILED here (OLD dies mid-reload); we still let
                # NEW try to come up. Final state is reconciled in
                # set_filter_chain after _next's ready_event fires.
                return
            # Map _SlotState → TranscoderState (1:1 in Task 1; Task 6 adds RELOADING logic)
            mapped = _SUBSTATE_TO_STATE[new_sub_state]
            self._set_state_locked(mapped, idle_reason=slot.idle_reason)

    def _on_next_substate_change(
        self, slot: _ProcessSlot, new_sub_state: _SlotState
    ) -> None:
        """Callback for the _next slot. We don't update public state here —
        set_filter_chain's blocked thread does that via _next.ready_event.
        """
        # No-op for Task 6: the blocked thread in set_filter_chain handles the
        # promotion/demotion decision after wait_until_ready() returns.
        pass

    def _aggregate_current_state_locked(self) -> TranscoderState:
        """Map _current's sub_state to a public TranscoderState. Caller MUST hold _state_lock."""
        if self._current is None:
            return TranscoderState.IDLE
        return _SUBSTATE_TO_STATE[self._current.sub_state]

    def set_filter_chain(self, chain: FilterChain) -> bool:
        """Hot-reload the filter chain.

        Blocks until NEW reaches READY (returns True; OLD killed; cast continues
        with the new chain) or NEW fails to reach READY (returns False; OLD still
        streaming; chain unchanged; last_reload_error set).

        Raises RuntimeError if called in IDLE/SPAWNING/WARMING/FAILED/TERMINATING/
        TERMINATED, or if a reload is already in progress.
        """
        with self._state_lock:
            if self._state in (
                TranscoderState.IDLE, TranscoderState.SPAWNING, TranscoderState.WARMING,
                TranscoderState.FAILED, TranscoderState.TERMINATING, TranscoderState.TERMINATED,
            ):
                raise RuntimeError(
                    f"set_filter_chain in state {self._state.name}; "
                    f"only READY/STREAMING/STALLED can initiate a reload"
                )
            if self._next is not None:
                raise RuntimeError("set_filter_chain: reload already in progress")
            # Allocate NEW slot
            self._slot_counter += 1
            new_dir = self._base_output_dir / f"v{self._slot_counter}"
            self._next = _ProcessSlot(
                output_dir=new_dir,
                warming_timeout=self._warming_timeout,
                stall_timeout=self._stall_timeout,
                poll_interval=self._poll_interval,
                on_sub_state_change=self._on_next_substate_change,
            )
            self._filter_chain_pending = chain
            self._set_state_locked(TranscoderState.RELOADING)

        # Build argv with the new chain
        argv = _build_argv(
            input_url=self._input_url,
            output_dir=new_dir,
            accel=self._accel,
            hls_segment_seconds=self._hls_segment_seconds,
            vf_fragment=chain.render(self._input_url),
        )
        # Capture a local reference: stop() may null self._next concurrently.
        # _ProcessSlot.start() is guarded so it won't create output_dir if the
        # slot has already been stopped.
        next_slot_local: _ProcessSlot = self._next  # type: ignore[assignment]
        next_slot_local.start(argv)

        # Block until NEW reaches READY or FAILED (or timeout).
        # Use the local reference — stop() may have set self._next to None.
        promoted = next_slot_local.wait_until_ready(timeout=self._warming_timeout)

        next_to_stop: Optional[_ProcessSlot] = None
        result: bool = False

        with self._state_lock:
            if self._state in (TranscoderState.TERMINATING, TranscoderState.TERMINATED):
                # Caller called stop() during the wait — clean up NEW
                next_to_stop = self._next
                self._next = None
                self._filter_chain_pending = None
                result = False
            elif not promoted or self._next.sub_state == _SlotState.FAILED:
                # Task 7 expands this path; happy path here doesn't exercise it.
                self._last_reload_error = self._next.idle_reason or "reload_warming_timed_out"
                next_to_stop = self._next
                self._next = None
                self._filter_chain_pending = None
                self._set_state_locked(self._aggregate_current_state_locked())
                result = False
            else:
                # Promote
                next_to_stop = self._current
                self._current = self._next
                self._next = None
                self._filter_chain = self._filter_chain_pending
                self._filter_chain_pending = None
                self._last_reload_error = None
                self._set_state_locked(self._aggregate_current_state_locked())
                result = True

        # Stop the discarded slot outside the lock (rmtree is slow on Windows).
        # Demote / abort branches kill fast (NEW is failed or being torn down);
        # promote keeps the full graceful drain so OLD can finish its in-flight segment.
        if next_to_stop is not None:
            next_to_stop.stop(drain_seconds=2.0 if result else 0.1)
        return result


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
    (re.compile(r"Stream specifier.*matches no streams", re.I), "subtitle_stream_missing"),
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
