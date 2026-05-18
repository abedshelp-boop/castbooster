"""Tests for the rewritten _handle_cast. All paths use a mocked
Transcoder + mocked CastManager — no real ffmpeg or pychromecast."""
import asyncio
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from castbooster.proxy import _build_app, _handle_cast
from castbooster.transcoder import TranscoderState


def _run(coro):
    return asyncio.run(coro)


class _FakeTranscoder:
    """Minimal Transcoder stand-in for unit tests."""

    def __init__(
        self, *,
        target_state: TranscoderState,
        idle_reason=None,
        ready_returns: bool = True,
        output_dir=None,
    ):
        self._state = TranscoderState.IDLE
        self._target = target_state
        self._idle_reason = idle_reason
        self._ready_returns = ready_returns
        self._output_dir = output_dir or Path("/tmp/fake")
        self.start_calls = 0
        self.stop_calls = 0
        self.wait_calls = 0

    @property
    def state(self): return self._state
    @property
    def idle_reason(self): return self._idle_reason
    @property
    def output_dir(self): return self._output_dir

    def start(self):
        self.start_calls += 1
        self._state = TranscoderState.WARMING

    def wait_until_ready(self, timeout=None):
        self.wait_calls += 1
        self._state = self._target
        return self._ready_returns

    def stop(self, drain_seconds=2.0):
        self.stop_calls += 1
        self._state = TranscoderState.TERMINATED


def _build_test_app(tmp_path):
    """Builds an app with a fake AccelProfile and a mock CastManager.play()."""
    app = _build_app("192.168.1.10")
    from castbooster.ffmpeg_probe import AccelProfile
    app["accel_profile"] = AccelProfile(
        ffmpeg_path="C:/fake/ffmpeg.exe",
        encoder="h264_nvenc", decoder="cuda", tier="nvidia",
    )
    cm = MagicMock()
    cm.play.return_value = "Living Room TV"
    cm.transcoder_failure_stats.return_value = {"total": 0, "by_reason": {}}
    app["cast_manager"] = cm
    return app


def test_cast_spawns_transcoder_and_calls_play_with_output_url(tmp_path):
    async def _go():
        app = _build_test_app(tmp_path)
        sess = app["session_store"].create("https://example.com/p.m3u8")
        cast_uuid = "12345678-1234-5678-1234-567812345678"
        fake_t = _FakeTranscoder(target_state=TranscoderState.READY)
        with patch("castbooster.proxy.Transcoder", lambda *a, **kw: fake_t):
            resp = await _handle_cast(app, {"token": sess.token, "castUuid": cast_uuid})
        assert resp["status"] == "ok"
        assert "/output/master.m3u8" in resp["playbackUrl"]
        assert fake_t.start_calls == 1
        assert fake_t.wait_calls == 1
        app["cast_manager"].play.assert_called_once()
        call = app["cast_manager"].play.call_args
        assert "/output/master.m3u8" in call.args[1]
        assert call.kwargs.get("on_session_end") is not None
        assert sess.transcoder is fake_t
        assert sess.passthrough_only is False
    _run(_go())


def test_cast_falls_back_to_passthrough_on_failed(tmp_path):
    async def _go():
        app = _build_test_app(tmp_path)
        sess = app["session_store"].create("https://example.com/p.m3u8")
        cast_uuid = "12345678-1234-5678-1234-567812345679"
        fake_t = _FakeTranscoder(
            target_state=TranscoderState.FAILED,
            idle_reason="warming_timed_out",
            ready_returns=False,
        )
        with patch("castbooster.proxy.Transcoder", lambda *a, **kw: fake_t):
            resp = await _handle_cast(app, {"token": sess.token, "castUuid": cast_uuid})
        assert resp["status"] == "ok"
        assert "/upstream/master.m3u8" in resp["playbackUrl"]
        assert fake_t.stop_calls == 1
        assert sess.passthrough_only is True
        assert sess.transcoder is None
        app["cast_manager"].record_transcoder_failure.assert_called_once_with(
            "warming_timed_out"
        )
    _run(_go())


def test_cast_passthrough_env_var_bypasses_transcoder(tmp_path, monkeypatch):
    async def _go():
        app = _build_test_app(tmp_path)
        sess = app["session_store"].create("https://example.com/p.m3u8")
        cast_uuid = "12345678-1234-5678-1234-56781234567a"
        constructed = []
        def _fake_ctor(*a, **kw):
            constructed.append(1)
            return _FakeTranscoder(target_state=TranscoderState.READY)
        monkeypatch.setenv("CASTBOOSTER_PASSTHROUGH", "1")
        with patch("castbooster.proxy.Transcoder", _fake_ctor):
            resp = await _handle_cast(app, {"token": sess.token, "castUuid": cast_uuid})
        assert resp["status"] == "ok"
        assert "/upstream/" in resp["playbackUrl"]
        assert constructed == []
        assert sess.passthrough_only is True
    _run(_go())


def test_cast_passthrough_only_session_skips_spawn(tmp_path):
    async def _go():
        app = _build_test_app(tmp_path)
        sess = app["session_store"].create("https://example.com/p.m3u8")
        sess.passthrough_only = True   # pre-set
        cast_uuid = "12345678-1234-5678-1234-56781234567b"
        constructed = []
        def _fake_ctor(*a, **kw):
            constructed.append(1)
            return _FakeTranscoder(target_state=TranscoderState.READY)
        with patch("castbooster.proxy.Transcoder", _fake_ctor):
            resp = await _handle_cast(app, {"token": sess.token, "castUuid": cast_uuid})
        assert resp["status"] == "ok"
        assert "/upstream/" in resp["playbackUrl"]
        assert constructed == []
    _run(_go())


def test_cast_no_accel_profile_forces_passthrough(tmp_path):
    async def _go():
        app = _build_test_app(tmp_path)
        app["accel_profile"] = None
        sess = app["session_store"].create("https://example.com/p.m3u8")
        cast_uuid = "12345678-1234-5678-1234-56781234567c"
        constructed = []
        def _fake_ctor(*a, **kw):
            constructed.append(1)
            return _FakeTranscoder(target_state=TranscoderState.READY)
        with patch("castbooster.proxy.Transcoder", _fake_ctor):
            resp = await _handle_cast(app, {"token": sess.token, "castUuid": cast_uuid})
        assert resp["status"] == "ok"
        assert "/upstream/" in resp["playbackUrl"]
        assert constructed == []
        assert sess.passthrough_only is True
    _run(_go())


def test_cast_unknown_token_returns_error(tmp_path):
    async def _go():
        app = _build_test_app(tmp_path)
        resp = await _handle_cast(app, {
            "token": "nonexistent", "castUuid": "12345678-1234-5678-1234-56781234567d",
        })
        assert resp["status"] == "error"
        assert "unknown token" in resp["detail"].lower()
    _run(_go())


def test_cast_uses_per_tier_warming_timeout_nvidia(tmp_path):
    """Hardware tier (nvidia) → warming_timeout=6.0."""
    async def _go():
        app = _build_test_app(tmp_path)
        sess = app["session_store"].create("https://example.com/p.m3u8")
        cast_uuid = "12345678-1234-5678-1234-56781234567e"
        constructed_kwargs = []
        def _fake_ctor(*args, **kwargs):
            constructed_kwargs.append(kwargs)
            return _FakeTranscoder(target_state=TranscoderState.READY)
        with patch("castbooster.proxy.Transcoder", _fake_ctor):
            await _handle_cast(app, {"token": sess.token, "castUuid": cast_uuid})
        assert constructed_kwargs[0]["warming_timeout"] == 6.0
    _run(_go())


def test_cast_uses_per_tier_warming_timeout_sw(tmp_path):
    """Software tier → warming_timeout=12.0."""
    async def _go():
        app = _build_test_app(tmp_path)
        from castbooster.ffmpeg_probe import AccelProfile
        app["accel_profile"] = AccelProfile(
            ffmpeg_path="C:/fake/ffmpeg.exe",
            encoder="libx264", decoder="none", tier="sw",
        )
        sess = app["session_store"].create("https://example.com/p.m3u8")
        cast_uuid = "12345678-1234-5678-1234-56781234567f"
        constructed_kwargs = []
        def _fake_ctor(*args, **kwargs):
            constructed_kwargs.append(kwargs)
            return _FakeTranscoder(target_state=TranscoderState.READY)
        with patch("castbooster.proxy.Transcoder", _fake_ctor):
            await _handle_cast(app, {"token": sess.token, "castUuid": cast_uuid})
        assert constructed_kwargs[0]["warming_timeout"] == 12.0
    _run(_go())


class _BlockingFakeTranscoder(_FakeTranscoder):
    """wait_until_ready blocks synchronously via time.sleep, mimicking the
    real threading.Event.wait() that caused the 2026-05-16 async-block bug."""

    WAIT_SECONDS = 0.5

    def wait_until_ready(self, timeout=None):
        self.wait_calls += 1
        time.sleep(self.WAIT_SECONDS)
        self._state = self._target
        return self._ready_returns


def test_cast_does_not_block_event_loop_during_warming(tmp_path):
    """Regression for the 2026-05-16 async-block root cause (spec §3.9).

    transcoder.wait_until_ready is synchronous (threading.Event.wait).  If
    _handle_cast awaits it without an executor, the asyncio event loop is
    frozen for the warming budget — ffmpeg's loopback fetches to
    /upstream/* can't be accepted, so the transcoder never reaches READY
    and every cast hits warming_timed_out.

    Verification: run _handle_cast concurrently with an asyncio.sleep(0.05)
    probe.  If the event loop is alive (fix in place), the probe fires
    ~50 ms after gather start.  If the loop is blocked (bug regressed), it
    fires only after the 500 ms synchronous wait completes.
    """
    async def _go():
        app = _build_test_app(tmp_path)
        sess = app["session_store"].create("https://example.com/p.m3u8")
        cast_uuid = "12345678-1234-5678-1234-567812345690"
        blocking_t = _BlockingFakeTranscoder(target_state=TranscoderState.READY)

        gather_start = time.monotonic()
        probe_fired_at = None

        async def _loop_probe():
            nonlocal probe_fired_at
            await asyncio.sleep(0.05)
            probe_fired_at = time.monotonic()

        with patch("castbooster.proxy.Transcoder", lambda *a, **kw: blocking_t):
            await asyncio.gather(
                _handle_cast(app, {"token": sess.token, "castUuid": cast_uuid}),
                _loop_probe(),
            )

        handler_elapsed = time.monotonic() - gather_start
        assert blocking_t.wait_calls == 1
        assert handler_elapsed >= _BlockingFakeTranscoder.WAIT_SECONDS - 0.05, (
            f"handler returned in {handler_elapsed:.3f}s — wait_until_ready was not "
            f"actually called for the full {_BlockingFakeTranscoder.WAIT_SECONDS}s"
        )
        probe_delay = probe_fired_at - gather_start
        # If the loop was blocked, probe_delay ≈ WAIT_SECONDS (0.5).
        # If the fix is in place, probe_delay ≈ 0.05.
        # 0.2s threshold gives ample slack for Windows asyncio.sleep jitter
        # (typically 15–30 ms granularity).
        assert probe_delay < 0.2, (
            f"event loop was blocked during transcoder.wait_until_ready: "
            f"asyncio.sleep(0.05) probe fired after {probe_delay:.3f}s "
            f"(expected <0.2s).  Did someone re-introduce a sync call to "
            f"transcoder.wait_until_ready in _handle_cast?  See spec §3.9."
        )
    _run(_go())
