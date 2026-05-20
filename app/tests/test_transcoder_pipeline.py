"""Multi-process slot lifecycle tests (Pillar 3.3).

Strategy: same as test_transcoder_lifecycle.py and test_transcoder_reload.py —
patch ``subprocess.Popen`` to return queued FakeFfmpegProcess instances.
Multi-proc mode queues ONE fake per slot — the encode ffmpeg. The side
task's inner subprocesses are NOT spawned because we substitute a
``FakeRIFEFilter`` whose ``side_task_factory`` is a pure-Python callable.

Tests are named with their M-* matrix ID per the parent P3 spec §10.6
state-enumeration acceptance criterion + the 2026-04-22 IDLE-race lesson.
"""
from __future__ import annotations

import io
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional
from unittest.mock import patch

import pytest

# Reuse the fakes + helpers from the existing test modules. They live in
# the same package so direct imports work.
from tests.test_transcoder_lifecycle import (
    FakeFfmpegProcess,
    make_sw_profile,
    _touch_segment,
    _touch_variant,
    _wait_for_state,
)


@pytest.fixture
def sw_profile():
    return make_sw_profile()


# ---------- Sequential Popen patching ---------------------------------------

class _FakePopenFactory:
    """Returns queued FakeFfmpegProcess instances in order from each Popen call.

    Multi-proc mode uses ONE queued fake per slot because the side task's
    inner Popens are bypassed by FakeRIFEFilter.
    """

    def __init__(self) -> None:
        self._fakes: List[FakeFfmpegProcess] = []
        self.popen_kwargs_history: List[dict] = []

    def queue(self, *fakes: FakeFfmpegProcess) -> None:
        self._fakes.extend(fakes)

    def __call__(self, *args, **kwargs) -> FakeFfmpegProcess:
        if not self._fakes:
            raise RuntimeError(
                "_FakePopenFactory exhausted — test queued too few fakes for "
                "the Popen call count"
            )
        # Capture kwargs so tests can assert bufsize=0 etc. on the encoder Popen.
        self.popen_kwargs_history.append(dict(kwargs))
        return self._fakes.pop(0)


@pytest.fixture
def fake_popen():
    """Yields a _FakePopenFactory; tests call factory.queue(...) to register fakes."""
    factory = _FakePopenFactory()
    with patch("castbooster.transcoder.subprocess.Popen", side_effect=factory):
        yield factory


def _make_ready(seg_dir: Path) -> None:
    """Mark a slot's output_dir as 'ready on disk' per _is_ready_on_disk()."""
    seg_dir.mkdir(parents=True, exist_ok=True)
    _touch_segment(seg_dir, 0)
    _touch_segment(seg_dir, 1)
    _touch_variant(seg_dir)


# ---------- FakeSideTask + FakeRIFEFilter -----------------------------------

class FakeSideTask:
    """Callable matching ``side_task_factory`` shape, with test-driver methods.

    Default behavior: block on ``ctx.cancel_event`` (mimics a healthy
    long-running side task). Tests can override behavior via constructor
    flags or by injecting a custom ``inner`` callable.

    Attributes captured for assertions:
        ctx_seen          — the SideTaskContext the wrapper passed in.
        started_event     — set when the side task enters its body.
        return_immediately — if True, returns without blocking.
        raise_with        — if set to an Exception instance, raises it on entry.
        write_on_start    — bytes to write to ctx.encoder_stdin before
                            blocking (default: nothing). Useful for
                            BrokenPipe testing.
        inner             — optional custom callable replacing the default
                            body. Receives the ctx.
    """

    def __init__(
        self,
        *,
        return_immediately: bool = False,
        raise_with: Optional[BaseException] = None,
        write_on_start: bytes = b"",
        inner: Optional[Callable] = None,
    ) -> None:
        self.return_immediately = return_immediately
        self.raise_with = raise_with
        self.write_on_start = write_on_start
        self.inner = inner
        self.ctx_seen = None
        self.started_event = threading.Event()
        self.finished_event = threading.Event()

    def __call__(self, ctx) -> None:
        self.ctx_seen = ctx
        self.started_event.set()
        try:
            if self.write_on_start:
                ctx.encoder_stdin.write(self.write_on_start)
            if self.raise_with is not None:
                raise self.raise_with
            if self.inner is not None:
                self.inner(ctx)
                return
            if self.return_immediately:
                return
            # Default: block on cancel_event. Use wait(timeout) so a runaway
            # test can't pin the daemon thread forever; in practice the
            # wrapper signals cancel_event during slot.stop().
            while not ctx.cancel_event.wait(timeout=0.05):
                pass
        finally:
            self.finished_event.set()


class FakeRIFEFilter:
    """Stub FilterStage returning a non-trivial PipelineSpec.

    Use this in P3.3 tests in place of the real ``RIFEFilter`` from P3.2
    so we don't need rife-ncnn-vulkan + ffmpeg binaries on disk to
    exercise slot mechanics.
    """

    def __init__(
        self,
        *,
        target_fps: int = 60,
        w: int = 320,
        h: int = 240,
        side_task: Optional[Callable] = None,
    ) -> None:
        self._fmt = f"rawvideo:yuv420p:{w}x{h}"
        self._tgt = target_fps
        self._side_task = side_task if side_task is not None else FakeSideTask()

    def render(self) -> str:
        return "null"

    def pipeline_spec(self):
        from castbooster.pipeline_spec import PipelineSpec
        return PipelineSpec(
            encoder_input_format=self._fmt,
            target_fps=self._tgt,
            side_task_factory=self._side_task,
        )


# ---------- M-LIFECYCLE-1: multi-proc SPAWNING → WARMING --------------------

def test_multiproc_start_reaches_warming_with_side_task_alive(
    tmp_path, sw_profile, fake_popen,
):
    """M-LIFECYCLE-1: Transcoder with a FakeRIFEFilter chain starts; reaches
    WARMING; side_task_thread is alive; encode Popen was called with bufsize=0."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain

    encode_fake = FakeFfmpegProcess()
    fake_popen.queue(encode_fake)

    side_task = FakeSideTask()
    rife = FakeRIFEFilter(side_task=side_task)

    t = Transcoder(
        input_url="http://upstream/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([rife]),
        src_fps_hint=24.0,
        _poll_interval=0.05,
    )
    t.start()
    try:
        # WARMING reached
        assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
        # Side task started + still alive (default FakeSideTask blocks on cancel)
        assert side_task.started_event.wait(timeout=1.0)
        slot = t._current
        assert slot is not None
        assert slot._side_task_thread is not None
        assert slot._side_task_thread.is_alive()
        # SideTaskContext was wired up
        assert side_task.ctx_seen is not None
        assert side_task.ctx_seen.input_url == "http://upstream/m.m3u8"
        assert side_task.ctx_seen.target_fps == 60
        assert side_task.ctx_seen.workdir.parent == tmp_path / "out" / "v1"
        # workdir was created
        assert side_task.ctx_seen.workdir.exists()
        # encode Popen got bufsize=0 (Q1 locked decision)
        assert any(
            kw.get("bufsize") == 0 for kw in fake_popen.popen_kwargs_history
        ), f"expected at least one bufsize=0 Popen, got: {fake_popen.popen_kwargs_history}"
    finally:
        encode_fake.set_exit(0)
        t.stop()


# ---------- M-LIFECYCLE-2: multi-proc WARMING → READY ------------------------

def test_multiproc_warming_to_ready_when_files_appear(
    tmp_path, sw_profile, fake_popen,
):
    """M-LIFECYCLE-2: From WARMING, dropping 2 segments + variant.m3u8 into
    the slot's output_dir promotes to READY. Side task remains alive."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain

    encode_fake = FakeFfmpegProcess()
    fake_popen.queue(encode_fake)

    side_task = FakeSideTask()
    rife = FakeRIFEFilter(side_task=side_task)

    t = Transcoder(
        input_url="http://upstream/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([rife]),
        src_fps_hint=24.0,
        _poll_interval=0.05,
    )
    t.start()
    try:
        assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
        # Drop 2 segs + variant to make _is_ready_on_disk return True
        seg_dir = t.output_dir  # base / v1
        _make_ready(seg_dir)
        # Watchdog poller should promote to READY within a tick or two
        assert _wait_for_state(t, TranscoderState.READY, timeout=1.5), \
            f"expected READY, got state={t.state}"
        # Side task should still be running (no failure)
        slot = t._current
        assert slot is not None
        assert slot._side_task_thread is not None
        assert slot._side_task_thread.is_alive()
        # No exception captured
        assert slot._side_task_exception is None
    finally:
        encode_fake.set_exit(0)
        t.stop()


# ---------- Pillar 5 metric placeholders -------------------------------------

def test_pillar5_metric_placeholders_default_to_none(tmp_path, sw_profile):
    """Transcoder exposes rife_fps_actual / rife_lag_seconds /
    vulkan_device_name properties, all None pre-Pillar 5 wiring."""
    from castbooster.transcoder import Transcoder

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
    )
    assert t.rife_fps_actual is None
    assert t.rife_lag_seconds is None
    assert t.vulkan_device_name is None


# ---------- M-FAIL-1: side task raises a generic exception -------------------

def test_side_task_generic_exception_marks_slot_failed(
    tmp_path, sw_profile, fake_popen,
):
    """M-FAIL-1: side task raises a generic Exception → slot transitions
    to FAILED with idle_reason='side_task_crashed'. side_task_exception is
    captured for postmortem."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain

    encode_fake = FakeFfmpegProcess()
    fake_popen.queue(encode_fake)

    boom = ValueError("simulated side-task bug")
    side_task = FakeSideTask(raise_with=boom)
    rife = FakeRIFEFilter(side_task=side_task)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([rife]),
        src_fps_hint=24.0,
        _poll_interval=0.05,
    )
    t.start()
    try:
        assert _wait_for_state(t, TranscoderState.FAILED, timeout=2.0), \
            f"expected FAILED, got state={t.state}"
        assert t.idle_reason == "side_task_crashed"
        slot = t._current
        assert slot is not None
        assert slot._side_task_exception is boom
    finally:
        encode_fake.set_exit(-9)  # release the stderr reader; encoder also "died"
        t.stop()


# ---------- M-FAIL-2: side task raises a wrapped subprocess RuntimeError -----

def test_side_task_subprocess_runtime_error_marks_slot_failed(
    tmp_path, sw_profile, fake_popen,
):
    """M-FAIL-2: side task raises a RuntimeError shaped like the one
    RIFEFilter._side_task surfaces when its inner decode/rife exits non-zero
    (see P3.2's app/castbooster/filters/interpolation.py final block).
    Routes to idle_reason='side_task_crashed' just like M-FAIL-1 — the
    same exception path."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain

    encode_fake = FakeFfmpegProcess()
    fake_popen.queue(encode_fake)

    boom = RuntimeError("decode ffmpeg exited 1: Connection refused")
    side_task = FakeSideTask(raise_with=boom)
    rife = FakeRIFEFilter(side_task=side_task)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([rife]),
        src_fps_hint=24.0,
        _poll_interval=0.05,
    )
    t.start()
    try:
        assert _wait_for_state(t, TranscoderState.FAILED, timeout=2.0)
        assert t.idle_reason == "side_task_crashed"
        slot = t._current
        assert slot is not None
        assert slot._side_task_exception is boom
        assert "decode ffmpeg exited 1" in str(slot._side_task_exception)
    finally:
        encode_fake.set_exit(-9)
        t.stop()


# ---------- M-FAIL-3: side task raises BrokenPipeError -> encoder_died -------

def test_side_task_broken_pipe_marks_slot_encoder_died(
    tmp_path, sw_profile, fake_popen,
):
    """M-FAIL-3: side task raises BrokenPipeError → slot FAILED with
    idle_reason='encoder_died' (distinct from generic side_task_crashed —
    Pillar 5 watchdog uses this signal to differentiate remediation)."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain

    encode_fake = FakeFfmpegProcess()
    fake_popen.queue(encode_fake)

    bp = BrokenPipeError("encoder closed stdin")
    side_task = FakeSideTask(raise_with=bp)
    rife = FakeRIFEFilter(side_task=side_task)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([rife]),
        src_fps_hint=24.0,
        _poll_interval=0.05,
    )
    t.start()
    try:
        assert _wait_for_state(t, TranscoderState.FAILED, timeout=2.0)
        assert t.idle_reason == "encoder_died"
        slot = t._current
        assert slot is not None
        assert slot._side_task_exception is bp
    finally:
        encode_fake.set_exit(-9)
        t.stop()


# ---------- M-FAIL-4: side task returns early (no exception, no cancel) -----

def test_side_task_returns_early_marks_slot_failed(
    tmp_path, sw_profile, fake_popen,
):
    """M-FAIL-4: side task returns cleanly without exception while still
    WARMING → poller catches via _check_side_task_locked → FAILED with
    idle_reason='side_task_returned_early'."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain

    encode_fake = FakeFfmpegProcess()
    fake_popen.queue(encode_fake)

    side_task = FakeSideTask(return_immediately=True)
    rife = FakeRIFEFilter(side_task=side_task)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([rife]),
        src_fps_hint=24.0,
        _poll_interval=0.05,
    )
    t.start()
    try:
        # The side task is `return_immediately=True` → it sets started_event,
        # then returns without raising. The poller picks this up on its
        # next tick (~50ms).
        assert side_task.finished_event.wait(timeout=1.0)
        assert _wait_for_state(t, TranscoderState.FAILED, timeout=2.0), \
            f"expected FAILED, got state={t.state}"
        assert t.idle_reason == "side_task_returned_early"
        slot = t._current
        assert slot is not None
        assert slot._side_task_exception is None  # no exception captured
    finally:
        encode_fake.set_exit(0)
        t.stop()


# ---------- M-STOP-1: stop() during multi-proc WARMING ----------------------

def test_stop_during_multiproc_warming_terminates_cleanly(
    tmp_path, sw_profile, fake_popen,
):
    """M-STOP-1: stop() while WARMING in multi-proc → TERMINATED.
    Side task exits cleanly (cancel_event); output_dir is removed."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain

    encode_fake = FakeFfmpegProcess()
    fake_popen.queue(encode_fake)

    side_task = FakeSideTask()  # blocks on cancel_event
    rife = FakeRIFEFilter(side_task=side_task)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([rife]),
        src_fps_hint=24.0,
        _poll_interval=0.05,
    )
    t.start()
    assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
    assert side_task.started_event.wait(timeout=1.0)
    v1 = tmp_path / "out" / "v1"
    assert v1.exists(), "v1 should exist after WARMING"

    # Schedule the fake encoder to "exit" shortly after the q\n write
    # so the drain returns promptly.
    def _let_drain_finish():
        time.sleep(0.05)
        encode_fake.set_exit(0)
    threading.Thread(target=_let_drain_finish, daemon=True).start()

    t.stop(drain_seconds=1.0)

    # Final state: TERMINATED
    assert t.state == TranscoderState.TERMINATED
    # Side task observed the cancel and exited
    assert side_task.finished_event.wait(timeout=2.0), \
        "side task did not see cancel_event in time"
    # output_dir cleaned up
    assert not v1.exists(), f"v1 survived stop(): exists"


# ---------- M-STOP-2: stop() during multi-proc STREAMING --------------------

def test_stop_during_multiproc_streaming_terminates_cleanly(
    tmp_path, sw_profile, fake_popen,
):
    """M-STOP-2: cast through WARMING → READY → STREAMING (multi-proc), then
    stop(). Final state TERMINATED; side task observes cancel; v1/ gone."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain

    encode_fake = FakeFfmpegProcess()
    fake_popen.queue(encode_fake)

    side_task = FakeSideTask()
    rife = FakeRIFEFilter(side_task=side_task)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([rife]),
        src_fps_hint=24.0,
        _poll_interval=0.05,
    )
    t.start()
    try:
        assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
        v1 = tmp_path / "out" / "v1"
        _make_ready(v1)
        assert _wait_for_state(t, TranscoderState.READY, timeout=1.5)
        _touch_segment(v1, 2)
        assert _wait_for_state(t, TranscoderState.STREAMING, timeout=1.5)
        # Now stop.
        def _let_drain():
            time.sleep(0.05)
            encode_fake.set_exit(0)
        threading.Thread(target=_let_drain, daemon=True).start()
        t.stop(drain_seconds=1.0)
        assert t.state == TranscoderState.TERMINATED
        assert side_task.finished_event.wait(timeout=2.0)
        assert not v1.exists()
    except Exception:
        encode_fake.set_exit(-9)
        raise


# ---------- M-RELOAD-1: hot-reload Noop -> stub-RIFE (promote) --------------

def test_reload_noop_to_stub_rife_promotes(tmp_path, sw_profile, fake_popen):
    """M-RELOAD-1: Transcoder running NoopFilter (single-Popen). Call
    set_filter_chain(FakeRIFEFilter). NEW slot uses multi-proc path
    (spawns encode + side task Thread). After NEW reaches READY, OLD is
    killed; promotion happens; state returns to READY/STREAMING; output_dir
    flips to v2."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain, NoopFilter

    old_fake = FakeFfmpegProcess()    # OLD encode (single-Popen)
    new_fake = FakeFfmpegProcess()    # NEW encode (multi-proc)
    fake_popen.queue(old_fake, new_fake)

    new_side_task = FakeSideTask()
    rife = FakeRIFEFilter(side_task=new_side_task)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([NoopFilter()]),
        src_fps_hint=24.0,
        _poll_interval=0.05,
    )
    t.start()
    try:
        # OLD to STREAMING
        v1 = tmp_path / "out" / "v1"
        assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
        _make_ready(v1)
        assert _wait_for_state(t, TranscoderState.READY, timeout=1.5)
        _touch_segment(v1, 2)
        assert _wait_for_state(t, TranscoderState.STREAMING, timeout=1.5)

        # Hot-reload to RIFE in a background thread (it blocks on NEW ready)
        result_q: queue.Queue = queue.Queue()

        def call_reload():
            try:
                r = t.set_filter_chain(FilterChain([rife]))
                result_q.put(("ok", r))
            except Exception as e:
                result_q.put(("err", e))

        th = threading.Thread(target=call_reload, daemon=True)
        th.start()

        # Reach RELOADING
        assert _wait_for_state(t, TranscoderState.RELOADING, timeout=1.5)
        # NEW side task started
        assert new_side_task.started_event.wait(timeout=1.5)

        # Make NEW ready
        v2 = tmp_path / "out" / "v2"
        _make_ready(v2)

        # Allow OLD's drain to complete by exiting cleanly
        old_fake.set_exit(0)

        # set_filter_chain returns True; cast continues on v2
        kind, value = result_q.get(timeout=3.0)
        assert kind == "ok", f"set_filter_chain raised: {value}"
        assert value is True
        assert t.state in (TranscoderState.READY, TranscoderState.STREAMING)
        assert t.output_dir == v2
        assert not v1.exists()
    finally:
        new_fake.set_exit(0)
        t.stop()


# ---------- M-RELOAD-2: hot-reload stub-RIFE -> Noop (promote) --------------

def test_reload_stub_rife_to_noop_promotes(tmp_path, sw_profile, fake_popen):
    """M-RELOAD-2: Transcoder running FakeRIFEFilter (multi-proc). Call
    set_filter_chain(NoopFilter). NEW slot uses single-Popen path.
    Promotion happens."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain, NoopFilter

    old_fake = FakeFfmpegProcess()    # OLD encode (multi-proc)
    new_fake = FakeFfmpegProcess()    # NEW encode (single-Popen)
    fake_popen.queue(old_fake, new_fake)

    old_side_task = FakeSideTask()
    rife = FakeRIFEFilter(side_task=old_side_task)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([rife]),
        src_fps_hint=24.0,
        _poll_interval=0.05,
    )
    t.start()
    try:
        v1 = tmp_path / "out" / "v1"
        assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
        assert old_side_task.started_event.wait(timeout=1.0)
        _make_ready(v1)
        assert _wait_for_state(t, TranscoderState.READY, timeout=1.5)
        _touch_segment(v1, 2)
        assert _wait_for_state(t, TranscoderState.STREAMING, timeout=1.5)

        result_q: queue.Queue = queue.Queue()

        def call_reload():
            try:
                r = t.set_filter_chain(FilterChain([NoopFilter()]))
                result_q.put(("ok", r))
            except Exception as e:
                result_q.put(("err", e))

        th = threading.Thread(target=call_reload, daemon=True)
        th.start()
        assert _wait_for_state(t, TranscoderState.RELOADING, timeout=1.5)

        v2 = tmp_path / "out" / "v2"
        _make_ready(v2)

        old_fake.set_exit(0)

        kind, value = result_q.get(timeout=3.0)
        assert kind == "ok", f"set_filter_chain raised: {value}"
        assert value is True
        # OLD side task observed cancel during OLD's stop()
        assert old_side_task.finished_event.wait(timeout=2.0)
        assert t.state in (TranscoderState.READY, TranscoderState.STREAMING)
        assert t.output_dir == v2
        assert not v1.exists()
    finally:
        new_fake.set_exit(0)
        t.stop()


# ---------- M-RELOAD-3: demote on NEW side_task_crashed ---------------------

def test_reload_demotes_on_new_side_task_crashed(
    tmp_path, sw_profile, fake_popen,
):
    """M-RELOAD-3: NEW slot's side task raises a generic Exception ->
    NEW transitions to FAILED -> set_filter_chain returns False;
    OLD continues serving; last_reload_error == 'side_task_crashed'."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain, NoopFilter

    old_fake = FakeFfmpegProcess()
    new_fake = FakeFfmpegProcess()
    fake_popen.queue(old_fake, new_fake)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([NoopFilter()]),
        src_fps_hint=24.0,
        warming_timeout=2.0,
        _poll_interval=0.05,
    )
    t.start()
    try:
        v1 = tmp_path / "out" / "v1"
        assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
        _make_ready(v1)
        assert _wait_for_state(t, TranscoderState.READY, timeout=1.5)

        boom = RuntimeError("simulated rife crash")
        bad_side_task = FakeSideTask(raise_with=boom)
        bad_rife = FakeRIFEFilter(side_task=bad_side_task)

        # set_filter_chain runs synchronously (NEW reaches FAILED quickly)
        result = t.set_filter_chain(FilterChain([bad_rife]))
        assert result is False
        assert t.last_reload_error == "side_task_crashed"
        # OLD still serving
        assert t.output_dir == v1
        assert t.state in (
            TranscoderState.READY, TranscoderState.STREAMING, TranscoderState.STALLED,
        )
        # NEW dir cleaned up
        assert not (tmp_path / "out" / "v2").exists()
    finally:
        old_fake.set_exit(0)
        new_fake.set_exit(-9)
        t.stop()


# ---------- M-RELOAD-4: demote on NEW encoder_died (BrokenPipe) -------------

def test_reload_demotes_on_new_encoder_died(
    tmp_path, sw_profile, fake_popen,
):
    """M-RELOAD-4: NEW slot's side task raises BrokenPipeError ->
    NEW transitions to FAILED with idle_reason='encoder_died' -> demote.
    last_reload_error reflects 'encoder_died'."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain, NoopFilter

    old_fake = FakeFfmpegProcess()
    new_fake = FakeFfmpegProcess()
    fake_popen.queue(old_fake, new_fake)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([NoopFilter()]),
        src_fps_hint=24.0,
        warming_timeout=2.0,
        _poll_interval=0.05,
    )
    t.start()
    try:
        v1 = tmp_path / "out" / "v1"
        assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
        _make_ready(v1)
        assert _wait_for_state(t, TranscoderState.READY, timeout=1.5)

        bp = BrokenPipeError("encoder stdin closed during reload")
        bad_side_task = FakeSideTask(raise_with=bp)
        bad_rife = FakeRIFEFilter(side_task=bad_side_task)

        result = t.set_filter_chain(FilterChain([bad_rife]))
        assert result is False
        assert t.last_reload_error == "encoder_died"
        assert t.output_dir == v1
        assert not (tmp_path / "out" / "v2").exists()
    finally:
        old_fake.set_exit(0)
        new_fake.set_exit(-9)
        t.stop()


# ---------- Cancel-event-set short-circuit -----------------------------------

def test_side_task_exception_during_cancel_does_not_mark_failed(
    tmp_path, sw_profile, fake_popen,
):
    """Regression for the wrapper's cancel-event-set short-circuit.

    If the slot is being torn down (cancel_event set) and the side task
    happens to raise on its way out, the wrapper must NOT transition the
    slot to FAILED — we're already TERMINATING/TERMINATED. Verified by
    setting cancel_event before raising.
    """
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain

    encode_fake = FakeFfmpegProcess()
    fake_popen.queue(encode_fake)

    # A side_task that BLOCKS first (lets us call stop() so cancel_event is set),
    # then on cancel_event arrival raises a "subprocess died" RuntimeError.
    # This mimics what happens if the side task's `finally` cleanup races.
    def _inner(ctx):
        # Wait briefly for cancel_event so the test can call stop() first.
        ctx.cancel_event.wait(timeout=2.0)
        # After cancel: raise. Wrapper must short-circuit.
        raise RuntimeError("teardown race")

    side_task = FakeSideTask(inner=_inner)
    rife = FakeRIFEFilter(side_task=side_task)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        filter_chain=FilterChain([rife]),
        src_fps_hint=24.0,
        _poll_interval=0.05,
    )
    t.start()
    assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
    assert side_task.started_event.wait(timeout=1.0)
    # Now call stop() — cancel_event is set, side task unblocks, raises,
    # wrapper short-circuits.
    encode_fake.set_exit(0)
    t.stop()
    # Final state is TERMINATED, NOT FAILED.
    assert t.state == TranscoderState.TERMINATED
