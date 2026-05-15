"""Unit tests for Transcoder.set_filter_chain() and the RELOADING state.

Strategy: same as test_transcoder_lifecycle.py — patch subprocess.Popen
to return queued FakeFfmpegProcess instances we drive directly. Each test
queues TWO fakes (one per slot) because a reload spawns two ffmpegs.
"""
from __future__ import annotations

import io
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, List
from unittest.mock import patch

import pytest

# Reuse FakeFfmpegProcess + helpers from test_transcoder_lifecycle.py.
# Tests in this module live in the same package so direct import works.
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


# ---------- Helper: sequential Popen patching --------------------------------

class _FakePopenFactory:
    """Returns queued FakeFfmpegProcess instances in order from each Popen call."""

    def __init__(self) -> None:
        self._fakes: List[FakeFfmpegProcess] = []

    def queue(self, *fakes: FakeFfmpegProcess) -> None:
        self._fakes.extend(fakes)

    def __call__(self, *args, **kwargs) -> FakeFfmpegProcess:
        if not self._fakes:
            raise RuntimeError(
                "_FakePopenFactory exhausted — test queued too few fakes for the Popen call count"
            )
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


# ---------- R14: subtitle_stream_missing fatal pattern -----------------------

def test_subtitle_stream_missing_pattern_classified():
    """The new fatal pattern for stream-spec-mismatch errors."""
    from castbooster.transcoder import _classify_stderr_line
    line = (
        "Stream specifier 's:0' in filtergraph description "
        "'subtitles=foo.mkv:si=0' matches no streams."
    )
    assert _classify_stderr_line(line) == "subtitle_stream_missing"


# ---------- R4: STREAMING → RELOADING → STREAMING happy path -----------------

def test_set_filter_chain_streaming_to_reloading(tmp_path, sw_profile, fake_popen):
    """Happy path: from STREAMING, set_filter_chain spawns NEW, NEW becomes
    READY, OLD gets killed, transcoder back to STREAMING; returns True."""
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain, NoopFilter

    old_fake = FakeFfmpegProcess()
    new_fake = FakeFfmpegProcess()
    fake_popen.queue(old_fake, new_fake)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        _poll_interval=0.05,
    )
    t.start()
    try:
        # 1) Bring OLD to STREAMING
        assert _wait_for_state(t, TranscoderState.WARMING, timeout=1.0)
        v1 = tmp_path / "out" / "v1"
        _make_ready(v1)
        assert _wait_for_state(t, TranscoderState.READY, timeout=1.0)
        _touch_segment(v1, 2)
        assert _wait_for_state(t, TranscoderState.STREAMING, timeout=1.0)

        # 2) Call set_filter_chain in a background thread (it blocks)
        result_q: queue.Queue = queue.Queue()

        def call_reload():
            try:
                r = t.set_filter_chain(FilterChain([NoopFilter()]))
                result_q.put(("ok", r))
            except Exception as e:
                result_q.put(("err", e))

        th = threading.Thread(target=call_reload, daemon=True)
        th.start()

        # 3) Wait for the transcoder to enter RELOADING
        assert _wait_for_state(t, TranscoderState.RELOADING, timeout=1.0)

        # 4) Make NEW ready
        v2 = tmp_path / "out" / "v2"
        _make_ready(v2)

        # 5) Set OLD to clean-exit so its stop() drain returns promptly
        old_fake.set_exit(0)

        # 6) The blocked set_filter_chain thread should now finish with True
        kind, value = result_q.get(timeout=2.0)
        assert kind == "ok", f"set_filter_chain raised: {value}"
        assert value is True

        # 7) State is back to STREAMING (or READY — both are stable serving states)
        assert t.state in (TranscoderState.READY, TranscoderState.STREAMING)
        # 8) _current is now v2
        assert t.output_dir == v2
        # 9) v1 was rmtree'd
        assert not v1.exists()
    finally:
        new_fake.set_exit(0)   # release new slot's stderr reader for stop()
        t.stop()


# ---------- R12: output_dir points at v1 during RELOADING --------------------

def test_output_dir_still_points_at_v1_during_reloading(tmp_path, sw_profile, fake_popen):
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain, NoopFilter

    old_fake = FakeFfmpegProcess()
    new_fake = FakeFfmpegProcess()
    fake_popen.queue(old_fake, new_fake)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        _poll_interval=0.05,
    )
    t.start()
    try:
        v1 = tmp_path / "out" / "v1"
        _make_ready(v1)
        assert _wait_for_state(t, TranscoderState.READY, timeout=1.0)

        # Kick off reload in background
        def call_reload():
            t.set_filter_chain(FilterChain([NoopFilter()]))

        th = threading.Thread(target=call_reload, daemon=True)
        th.start()

        # Wait until RELOADING
        assert _wait_for_state(t, TranscoderState.RELOADING, timeout=1.0)

        # NEW has NOT yet been made ready — _next is still WARMING
        assert t.output_dir == v1, f"expected v1, got {t.output_dir}"

        # Now promote
        v2 = tmp_path / "out" / "v2"
        _make_ready(v2)
        old_fake.set_exit(0)
        th.join(timeout=2.0)
        assert not th.is_alive(), "reload thread did not finish in time"
    finally:
        new_fake.set_exit(0)
        t.stop()


# ---------- R13: master_playlist updates on promote --------------------------

def test_master_playlist_path_updates_on_promote(tmp_path, sw_profile, fake_popen):
    from castbooster.transcoder import Transcoder, TranscoderState
    from castbooster.filter_chain import FilterChain, NoopFilter

    old_fake = FakeFfmpegProcess()
    new_fake = FakeFfmpegProcess()
    fake_popen.queue(old_fake, new_fake)

    t = Transcoder(
        input_url="http://x/m.m3u8",
        output_dir=tmp_path / "out",
        accel=sw_profile,
        _poll_interval=0.05,
    )
    t.start()
    try:
        v1 = tmp_path / "out" / "v1"
        v2 = tmp_path / "out" / "v2"
        _make_ready(v1)
        assert _wait_for_state(t, TranscoderState.READY, timeout=1.0)
        assert t.master_playlist == v1 / "master.m3u8"

        def call_reload():
            t.set_filter_chain(FilterChain([NoopFilter()]))

        th = threading.Thread(target=call_reload, daemon=True)
        th.start()
        assert _wait_for_state(t, TranscoderState.RELOADING, timeout=1.0)
        _make_ready(v2)
        old_fake.set_exit(0)
        th.join(timeout=2.0)
        assert not th.is_alive(), "reload thread did not finish in time"

        assert t.master_playlist == v2 / "master.m3u8"
    finally:
        new_fake.set_exit(0)
        t.stop()
