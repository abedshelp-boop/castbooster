"""Unit tests for castbooster.transcoder.

Strategy: patch subprocess.Popen to return a FakeFfmpegProcess we drive
directly from the test. Each test manipulates the fake (queue stderr lines,
set exit code, accept stdin) AND directly touches files in the output dir;
the 250ms poller picks up the changes. Tests pass _poll_interval=0.05 to
keep CI fast.
"""
from __future__ import annotations

import io
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest


# ---------- FakeFfmpegProcess ------------------------------------------------

class _FakeStderr:
    """Blocking-iterator stderr backed by a queue. yields bytes lines until
    .close() pushes the sentinel and StopIteration is raised."""

    _SENTINEL = object()

    def __init__(self) -> None:
        self._q: "queue.Queue[object]" = queue.Queue()

    def queue_line(self, line: bytes) -> None:
        self._q.put(line)

    def close(self) -> None:
        self._q.put(self._SENTINEL)

    def __iter__(self) -> "_FakeStderr":
        return self

    def __next__(self) -> bytes:
        try:
            item = self._q.get(timeout=30.0)
        except queue.Empty:
            raise RuntimeError(
                "_FakeStderr blocked >30s with no item — "
                "test forgot to call FakeFfmpegProcess.set_exit() or .queue_stderr()"
            ) from None
        if item is self._SENTINEL:
            raise StopIteration
        assert isinstance(item, bytes)
        return item


class FakeFfmpegProcess:
    """Minimal subprocess.Popen stand-in. Tests drive .set_exit() and
    .queue_stderr() to simulate ffmpeg behavior."""

    def __init__(self) -> None:
        self.stdin = io.BytesIO()
        self.stderr = _FakeStderr()
        self._exit_code: Optional[int] = None
        self._exit_event = threading.Event()

    # --- Popen-compatible surface ---
    def poll(self) -> Optional[int]:
        return self._exit_code

    def wait(self, timeout: Optional[float] = None) -> int:
        if self._exit_event.wait(timeout):
            assert self._exit_code is not None
            return self._exit_code
        raise subprocess.TimeoutExpired(cmd="fake-ffmpeg", timeout=timeout)

    def kill(self) -> None:
        self.set_exit(-9)

    # --- Test-driver surface ---
    def set_exit(self, code: int) -> None:
        if self._exit_code is None:
            self._exit_code = code
            self.stderr.close()
            self._exit_event.set()

    def queue_stderr(self, line: str) -> None:
        if not line.endswith("\n"):
            line += "\n"
        self.stderr.queue_line(line.encode("utf-8"))


# ---------- Smoke test for the helper itself ---------------------------------

def test_fake_ffmpeg_process_basic_lifecycle():
    fake = FakeFfmpegProcess()
    assert fake.poll() is None

    # Queue a stderr line and consume it
    fake.queue_stderr("hello")
    assert next(iter(fake.stderr)) == b"hello\n"

    # Set exit code → poll/wait return it
    fake.set_exit(0)
    assert fake.poll() == 0
    assert fake.wait(timeout=0.1) == 0

    # Stderr iterator now stops cleanly
    with pytest.raises(StopIteration):
        next(iter(fake.stderr))


def test_fake_ffmpeg_kill_sets_exit_neg9():
    fake = FakeFfmpegProcess()
    fake.kill()
    assert fake.poll() == -9


def test_fake_ffmpeg_wait_times_out_when_not_exited():
    fake = FakeFfmpegProcess()
    with pytest.raises(subprocess.TimeoutExpired):
        fake.wait(timeout=0.05)


# ---------- Test fixtures + helpers ------------------------------------------

from castbooster.ffmpeg_probe import AccelProfile


def make_sw_profile() -> AccelProfile:
    return AccelProfile(
        ffmpeg_path="C:/fake/ffmpeg.exe",
        encoder="libx264",
        decoder="d3d11va",
        tier="sw",
        available_encoders=["libx264"],
        available_hwaccels=["d3d11va"],
        ffmpeg_version="8.1-test",
    )


@pytest.fixture
def sw_profile():
    return make_sw_profile()


# ---------- Construction / IDLE ----------------------------------------------

def test_initial_state_is_idle(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder, TranscoderState
    t = Transcoder(
        input_url="http://example.local/master.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
    )
    assert t.state == TranscoderState.IDLE
    assert t.idle_reason is None
    assert t.exit_code is None


# ---------- ffmpeg argv construction -----------------------------------------

def test_command_uses_libx264_flags_for_sw_tier(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder
    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
    )
    argv = t._build_argv()
    assert "C:/fake/ffmpeg.exe" == argv[0]
    assert "-c:v" in argv
    enc_idx = argv.index("-c:v")
    assert argv[enc_idx + 1] == "libx264"
    # libx264-specific flags from the spec table
    def assert_adjacent(flag: str, value: str) -> None:
        idx = argv.index(flag)
        assert argv[idx + 1] == value, (
            f"{flag} should be followed by {value!r}, got {argv[idx + 1]!r}"
        )
    assert_adjacent("-preset", "veryfast")
    assert_adjacent("-tune", "zerolatency")
    assert_adjacent("-pix_fmt", "yuv420p")
    assert_adjacent("-vf", "null")
    assert_adjacent("-c:a", "copy")
    assert_adjacent("-hls_time", "2")
    assert_adjacent("-hls_list_size", "6")
    assert_adjacent("-f", "hls")
    # Input URL passed through
    assert "http://x/m.m3u8" in argv


def test_command_omits_hwaccel_when_decoder_is_none(tmp_path, sw_profile):
    from dataclasses import replace
    from castbooster.transcoder import Transcoder
    profile = replace(sw_profile, decoder="none")
    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=profile,
    )
    argv = t._build_argv()
    assert "-hwaccel" not in argv


def test_command_includes_hwaccel_when_decoder_available(tmp_path, sw_profile):
    from dataclasses import replace
    from castbooster.transcoder import Transcoder
    profile = replace(sw_profile, decoder="cuda", encoder="h264_nvenc", tier="nvidia")
    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=profile,
    )
    argv = t._build_argv()
    assert "-hwaccel" in argv
    hw_idx = argv.index("-hwaccel")
    assert argv[hw_idx + 1] == "cuda"
    enc_idx = argv.index("-c:v")
    assert argv[enc_idx + 1] == "h264_nvenc"
    # nvenc-specific flags from the spec table
    preset_idx = argv.index("-preset")
    assert argv[preset_idx + 1] == "p5"
    rc_idx = argv.index("-rc")
    assert argv[rc_idx + 1] == "cbr"


def test_command_segment_paths_under_output_dir(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder
    out = tmp_path / "out"
    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=out,
        accel=sw_profile,
    )
    argv = t._build_argv()
    seg_idx = argv.index("-hls_segment_filename")
    assert str(out / "seg_%05d.ts") == argv[seg_idx + 1]
    assert str(out / "variant.m3u8") == argv[-1]


# ---------- Master playlist --------------------------------------------------

def test_master_playlist_written_on_start(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder
    fake = FakeFfmpegProcess()
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=tmp_path / "out",
            accel=sw_profile,
            _poll_interval=0.05,
        )
        t.start()
        try:
            master = tmp_path / "out" / "master.m3u8"
            assert master.exists()
            contents = master.read_text(encoding="utf-8")
            assert contents.startswith("#EXTM3U")
            assert "#EXT-X-VERSION:3" in contents
            assert "BANDWIDTH=3000000" in contents
            assert "RESOLUTION=1280x720" in contents
            assert contents.rstrip().endswith("variant.m3u8")
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


# ---------- start() lifecycle ------------------------------------------------

def test_start_transitions_to_warming(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake) as popen_mock:
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=tmp_path / "out",
            accel=sw_profile,
            _poll_interval=0.05,
        )
        t.start()
        try:
            # WARMING is set synchronously in start() before threads launch.
            # Sleep here gives the daemon threads time to be scheduled so
            # stop() in the finally block finds them alive (relevant once
            # later rounds add thread.join() to stop()).
            time.sleep(0.1)
            assert t.state == TranscoderState.WARMING
            assert popen_mock.call_count == 1
            # Verify Popen was called with our argv
            args = popen_mock.call_args.args[0]
            assert args[0] == sw_profile.ffmpeg_path
            # stdin=PIPE, stderr=PIPE so we can drain on stop + read errors
            kwargs = popen_mock.call_args.kwargs
            assert kwargs["stdin"] == subprocess.PIPE
            assert kwargs["stderr"] == subprocess.PIPE
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


def test_start_raises_when_called_twice(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder
    fake = FakeFfmpegProcess()
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=tmp_path / "out",
            accel=sw_profile,
            _poll_interval=0.05,
        )
        t.start()
        try:
            with pytest.raises(RuntimeError):
                t.start()
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


# ---------- _classify_stderr_line --------------------------------------------

def test_classify_stderr_returns_none_for_normal_line():
    from castbooster.transcoder import _classify_stderr_line
    assert _classify_stderr_line(
        "frame=   12 fps= 12 q=21.0 size=      16kB time=00:00:00.96"
    ) is None
    assert _classify_stderr_line("") is None
    assert _classify_stderr_line("Stream mapping:") is None


def test_classify_stderr_matches_nvenc_failure():
    from castbooster.transcoder import _classify_stderr_line
    assert _classify_stderr_line(
        "[h264_nvenc @ 0x55] No NVENC capable devices found"
    ) == "hwaccel_unavailable"
    assert _classify_stderr_line(
        "Cannot load nvcuda.dll"
    ) == "hwaccel_unavailable"
    assert _classify_stderr_line(
        "qsv: hardware not supported"
    ) == "hwaccel_unavailable"


def test_classify_stderr_matches_input_failure():
    from castbooster.transcoder import _classify_stderr_line
    assert _classify_stderr_line(
        "http://x/m.m3u8: Connection refused"
    ) == "input_unreachable"
    assert _classify_stderr_line(
        "HTTP error 404 Not Found"
    ) == "input_unreachable"
    assert _classify_stderr_line(
        "Invalid data found when processing input"
    ) == "input_unreachable"


def test_classify_stderr_matches_encoder_init_failure():
    from castbooster.transcoder import _classify_stderr_line
    assert _classify_stderr_line(
        "Error opening encoder for stream 0"
    ) == "encoder_init_failed"
    assert _classify_stderr_line(
        "Failed to open codec libx264"
    ) == "encoder_init_failed"


# ---------- WARMING -> FAILED via stderr --------------------------------------

def _wait_for_state(t, target, timeout=2.0):
    """Helper: poll until state matches OR timeout elapses."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if t.state == target:
            return True
        time.sleep(0.02)
    return False


def test_warming_to_failed_on_stderr_nvenc_pattern(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=tmp_path / "out",
            accel=sw_profile,
            _poll_interval=0.05,
        )
        t.start()
        try:
            # Wait for WARMING to settle
            assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
            # Emit a fatal stderr line
            fake.queue_stderr("[h264_nvenc @ 0x55] No NVENC capable devices found")
            assert _wait_for_state(t, TranscoderState.FAILED, timeout=1.0), \
                f"state={t.state}"
            assert t.idle_reason == "hwaccel_unavailable"
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


def test_warming_to_failed_on_stderr_input_pattern(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=tmp_path / "out",
            accel=sw_profile,
            _poll_interval=0.05,
        )
        t.start()
        try:
            assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
            fake.queue_stderr("http://x/m.m3u8: HTTP error 404 Not Found")
            assert _wait_for_state(t, TranscoderState.FAILED, timeout=1.0), \
                f"state={t.state}"
            assert t.idle_reason == "input_unreachable"
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


# ---------- WARMING -> READY -------------------------------------------------

def _touch_segment(output_dir: Path, n: int) -> None:
    """Create a fake seg_NNNNN.ts file with a unique mtime."""
    seg = output_dir / f"seg_{n:05d}.ts"
    seg.write_bytes(b"\x00" * 16)
    # Slight delay so subsequent touches get distinguishable mtimes
    time.sleep(0.01)


def _touch_variant(output_dir: Path, contents: str = "#EXTM3U\n") -> None:
    (output_dir / "variant.m3u8").write_text(contents, encoding="utf-8")


def test_warming_to_ready_when_files_appear(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    out = tmp_path / "out"
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=out,
            accel=sw_profile,
            _poll_interval=0.05,
        )
        t.start()
        try:
            assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
            # Simulate ffmpeg writing output files
            _touch_segment(out, 0)
            _touch_segment(out, 1)
            _touch_variant(out)
            assert _wait_for_state(t, TranscoderState.READY, timeout=1.0), \
                f"state={t.state}"
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


# ---------- WARMING -> FAILED on timeout -------------------------------------

def test_warming_to_failed_on_timeout(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=tmp_path / "out",
            accel=sw_profile,
            warming_timeout=0.2,    # tight: fail fast
            _poll_interval=0.05,
        )
        t.start()
        try:
            assert _wait_for_state(t, TranscoderState.FAILED, timeout=1.0), \
                f"state={t.state}"
            assert t.idle_reason == "warming_timed_out"
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


# ---------- WARMING -> FAILED on early exit ----------------------------------

def test_warming_to_failed_on_early_exit(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=tmp_path / "out",
            accel=sw_profile,
            _poll_interval=0.05,
        )
        t.start()
        try:
            assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
            # Subprocess dies with no fatal stderr pattern beforehand
            fake.set_exit(1)
            assert _wait_for_state(t, TranscoderState.FAILED, timeout=1.0), \
                f"state={t.state}"
            assert t.idle_reason == "subprocess_died_early"
            assert t.exit_code == 1
        finally:
            t.stop()


# ---------- READY -> STREAMING -----------------------------------------------

def test_ready_to_streaming_on_new_seg(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    out = tmp_path / "out"
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=out,
            accel=sw_profile,
            _poll_interval=0.05,
        )
        t.start()
        try:
            assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
            _touch_segment(out, 0)
            _touch_segment(out, 1)
            _touch_variant(out)
            assert _wait_for_state(t, TranscoderState.READY, timeout=1.0)
            # Now a 3rd segment appears → STREAMING
            _touch_segment(out, 2)
            assert _wait_for_state(t, TranscoderState.STREAMING, timeout=1.0), \
                f"state={t.state}"
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


# ---------- STREAMING <-> STALLED --------------------------------------------

def test_streaming_to_stalled_on_quiet(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    out = tmp_path / "out"
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=out,
            accel=sw_profile,
            stall_timeout=0.25,        # tight: stall fast
            _poll_interval=0.05,
        )
        t.start()
        try:
            assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
            _touch_segment(out, 0); _touch_segment(out, 1); _touch_variant(out)
            assert _wait_for_state(t, TranscoderState.READY, timeout=1.0)
            _touch_segment(out, 2)
            assert _wait_for_state(t, TranscoderState.STREAMING, timeout=1.0)
            # Now no new segs → STALLED after stall_timeout
            assert _wait_for_state(t, TranscoderState.STALLED, timeout=1.0), \
                f"state={t.state}"
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


def test_stalled_recovers_to_streaming(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    out = tmp_path / "out"
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=out,
            accel=sw_profile,
            stall_timeout=0.25,
            _poll_interval=0.05,
        )
        t.start()
        try:
            assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
            _touch_segment(out, 0); _touch_segment(out, 1); _touch_variant(out)
            assert _wait_for_state(t, TranscoderState.READY, timeout=1.0)
            _touch_segment(out, 2)
            assert _wait_for_state(t, TranscoderState.STREAMING, timeout=1.0)
            assert _wait_for_state(t, TranscoderState.STALLED, timeout=1.0)
            # New seg → back to STREAMING
            _touch_segment(out, 3)
            assert _wait_for_state(t, TranscoderState.STREAMING, timeout=1.0), \
                f"state={t.state}"
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


# ---------- wait_until_ready -------------------------------------------------

def test_wait_until_ready_true_when_ready(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    out = tmp_path / "out"
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=out,
            accel=sw_profile,
            _poll_interval=0.05,
        )
        t.start()
        try:
            # Have a background producer race against wait_until_ready
            def produce():
                time.sleep(0.1)
                _touch_segment(out, 0); _touch_segment(out, 1); _touch_variant(out)
            threading.Thread(target=produce, daemon=True).start()
            assert t.wait_until_ready(timeout=2.0) is True
            assert t.state in (TranscoderState.READY, TranscoderState.STREAMING)
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


def test_wait_until_ready_false_on_failure(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=tmp_path / "out",
            accel=sw_profile,
            _poll_interval=0.05,
        )
        t.start()
        try:
            fake.queue_stderr("No NVENC capable devices found")
            assert t.wait_until_ready(timeout=2.0) is False
            assert t.state == TranscoderState.FAILED
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


def test_wait_until_ready_false_on_timeout(tmp_path, sw_profile):
    from castbooster.transcoder import Transcoder
    fake = FakeFfmpegProcess()
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=tmp_path / "out",
            accel=sw_profile,
            warming_timeout=10.0,    # poller wouldn't trip in time
            _poll_interval=0.05,
        )
        t.start()
        try:
            assert t.wait_until_ready(timeout=0.2) is False
        finally:
            fake.set_exit(0)   # release stderr reader so stop()'s join returns promptly
            t.stop()


# ---------- stop() graceful drain --------------------------------------------

def test_stop_drains_via_stdin_q(tmp_path, sw_profile):
    """stop() must write b"q\\n" to ffmpeg's stdin (its documented clean
    shutdown). Reliable on Windows where SIGTERM is flaky."""
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=tmp_path / "out",
            accel=sw_profile,
            _poll_interval=0.05,
        )
        t.start()
        # Have the fake "exit" shortly after seeing the q so drain completes
        def graceful():
            # Wait briefly, then accept the shutdown
            time.sleep(0.05)
            fake.set_exit(0)
        threading.Thread(target=graceful, daemon=True).start()
        t.stop(drain_seconds=1.0)
        assert fake.stdin.getvalue() == b"q\n"
        assert t.state == TranscoderState.TERMINATED


def test_stop_kills_after_drain_timeout(tmp_path, sw_profile):
    """Fake ignores q\\n → stop() must call process.kill() after drain_seconds."""
    from castbooster.transcoder import Transcoder, TranscoderState
    fake = FakeFfmpegProcess()
    with patch("castbooster.transcoder.subprocess.Popen", return_value=fake):
        t = Transcoder(
            input_url="http://x/m.m3u8",
            output_dir=tmp_path / "out",
            accel=sw_profile,
            _poll_interval=0.05,
        )
        t.start()
        # Fake never sets exit on its own — stop() must escalate
        start = time.monotonic()
        t.stop(drain_seconds=0.1)
        elapsed = time.monotonic() - start
        assert t.state == TranscoderState.TERMINATED
        assert fake.poll() == -9, "kill() should set exit code to -9"
        assert elapsed < 2.0, f"stop() took {elapsed:.2f}s — escalation too slow"
