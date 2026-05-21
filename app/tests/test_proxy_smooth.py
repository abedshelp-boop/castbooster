"""Tests for the P3.4 'Smooth motion' surface area of proxy.py:
    - _handle_capabilities     (is_pro + vulkan_available probe)
    - _build_filter_chain      (spec §4.2 decision tree)
    - _handle_cast extension   (reads enable_smooth, builds chain via helper)
    - _handle_set_filter_chain (mid-cast hot-reload + failure demote)
    - _handle_get_session_status (drains pending_warnings)
    - _watchdog_tick           (FAILED slot → demote-to-NoopFilter + warn)

All paths use mocks for license, ffmpeg_probe, CastManager, and Transcoder
so the suite runs offline."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from castbooster.ffmpeg_probe import AccelProfile, InputVideoInfo
from castbooster.filter_chain import FilterChain, NoopFilter
from castbooster.proxy import (
    _build_app,
    _build_filter_chain,
    _handle_capabilities,
    _handle_cast,
    _handle_get_session_status,
    _handle_set_filter_chain,
    _watchdog_tick,
)
from castbooster.receiver_caps import ReceiverCaps
from castbooster.transcoder import TranscoderState


def _run(coro):
    return asyncio.run(coro)


# ---- shared fixtures -------------------------------------------------------


def _video_60fps_hd():
    return InputVideoInfo(fps=24.0, width=1920, height=1080, pix_fmt="yuv420p")


def _video_vfr():
    return InputVideoInfo(fps=None, width=1920, height=1080, pix_fmt="yuv420p")


def _video_already_60fps():
    return InputVideoInfo(fps=60.0, width=1920, height=1080, pix_fmt="yuv420p")


def _caps_ultra_60():
    return ReceiverCaps(
        tier="ultra", max_width=3840, max_height=2160, max_fps=60,
        supports_h265=True, supports_av1=False,
        supports_ac3=True, supports_eac3=True, audio_only=False,
    )


def _caps_30cap():
    return ReceiverCaps(
        tier="3rd_gen", max_width=1920, max_height=1080, max_fps=30,
        supports_h265=False, supports_av1=False,
        supports_ac3=False, supports_eac3=False, audio_only=False,
    )


def _caps_audio_only():
    return ReceiverCaps(
        tier="audio_only", max_width=0, max_height=0, max_fps=0,
        supports_h265=False, supports_av1=False,
        supports_ac3=False, supports_eac3=False, audio_only=True,
    )


def _build_test_app():
    """App with a fake AccelProfile and MagicMock CastManager.

    Same shape as test_handle_cast._build_test_app, exported here so the
    P3.4 tests stay self-contained.
    """
    app = _build_app("192.168.1.10")
    app["accel_profile"] = AccelProfile(
        ffmpeg_path="C:/fake/ffmpeg.exe",
        encoder="h264_nvenc", decoder="cuda", tier="nvidia",
    )
    cm = MagicMock()
    cm.play.return_value = "Living Room TV"
    cm.transcoder_failure_stats.return_value = {"total": 0, "by_reason": {}}
    cm.capabilities.return_value = _caps_ultra_60()
    app["cast_manager"] = cm
    return app


# ---- _handle_capabilities --------------------------------------------------


def test_capabilities_both_true():
    async def _go():
        app = _build_test_app()
        with patch("castbooster.proxy.license.is_pro", return_value=True), \
             patch("castbooster.proxy.license.vulkan_available", return_value=True):
            resp = await _handle_capabilities(app, {"type": "capabilities"})
        assert resp == {
            "type": "capabilities",
            "is_pro": True,
            "vulkan_available": True,
        }
    _run(_go())


def test_capabilities_no_vulkan():
    async def _go():
        app = _build_test_app()
        with patch("castbooster.proxy.license.is_pro", return_value=True), \
             patch("castbooster.proxy.license.vulkan_available", return_value=False):
            resp = await _handle_capabilities(app, {"type": "capabilities"})
        assert resp["is_pro"] is True
        assert resp["vulkan_available"] is False
    _run(_go())


def test_capabilities_no_pro():
    async def _go():
        app = _build_test_app()
        with patch("castbooster.proxy.license.is_pro", return_value=False), \
             patch("castbooster.proxy.license.vulkan_available", return_value=True):
            resp = await _handle_capabilities(app, {"type": "capabilities"})
        assert resp["is_pro"] is False
        assert resp["vulkan_available"] is True
    _run(_go())


# ---- _build_filter_chain (spec §4.2 decision tree) -------------------------


def test_build_filter_chain_audio_only_returns_noop():
    chain, msg = _build_filter_chain(
        enable_smooth=True, caps=_caps_audio_only(), video=_video_60fps_hd(),
    )
    assert isinstance(chain.stages[0], NoopFilter)
    assert msg is None


def test_build_filter_chain_disabled_returns_noop():
    chain, msg = _build_filter_chain(
        enable_smooth=False, caps=_caps_ultra_60(), video=_video_60fps_hd(),
    )
    assert isinstance(chain.stages[0], NoopFilter)
    assert msg is None


def test_build_filter_chain_no_pro_returns_noop():
    with patch("castbooster.proxy.license.is_pro", return_value=False):
        chain, msg = _build_filter_chain(
            enable_smooth=True, caps=_caps_ultra_60(), video=_video_60fps_hd(),
        )
    assert isinstance(chain.stages[0], NoopFilter)
    assert msg is None


def test_build_filter_chain_probe_failed_returns_noop_with_info():
    """Pillar 3.5: probe failure with smooth ON and Pro → surfaces info_message."""
    from castbooster.proxy import _PROBE_FAILED_INFO
    with patch("castbooster.proxy.license.is_pro", return_value=True):
        chain, msg = _build_filter_chain(
            enable_smooth=True, caps=_caps_ultra_60(), video=None,
        )
    assert isinstance(chain.stages[0], NoopFilter)
    assert msg == _PROBE_FAILED_INFO


def test_build_filter_chain_vfr_source_returns_noop_with_info():
    with patch("castbooster.proxy.license.is_pro", return_value=True):
        chain, msg = _build_filter_chain(
            enable_smooth=True, caps=_caps_ultra_60(), video=_video_vfr(),
        )
    assert isinstance(chain.stages[0], NoopFilter)
    assert msg is not None
    assert "frame rate" in msg.lower()


def test_build_filter_chain_source_already_at_target_returns_noop():
    with patch("castbooster.proxy.license.is_pro", return_value=True):
        chain, msg = _build_filter_chain(
            enable_smooth=True, caps=_caps_ultra_60(),
            video=_video_already_60fps(),
        )
    assert isinstance(chain.stages[0], NoopFilter)
    assert msg is None


def test_build_filter_chain_happy_path_returns_rife():
    fake_rife = MagicMock(name="RIFEFilter")
    fake_rife.pipeline_spec.return_value = MagicMock()
    fake_rife.render.return_value = "null"
    with patch("castbooster.proxy.license.is_pro", return_value=True), \
         patch("castbooster.proxy.RIFEFilter", return_value=fake_rife) as ctor:
        chain, msg = _build_filter_chain(
            enable_smooth=True, caps=_caps_ultra_60(), video=_video_60fps_hd(),
        )
    assert chain.stages[0] is fake_rife
    assert msg is None
    ctor.assert_called_once_with(
        source_fps=24.0, target_fps=60, width=1920, height=1080,
    )


def test_build_filter_chain_30cap_target():
    """24fps source on a 30-cap Chromecast → RIFEFilter aiming at 30fps."""
    fake_rife = MagicMock(name="RIFEFilter")
    fake_rife.pipeline_spec.return_value = MagicMock()
    fake_rife.render.return_value = "null"
    with patch("castbooster.proxy.license.is_pro", return_value=True), \
         patch("castbooster.proxy.RIFEFilter", return_value=fake_rife) as ctor:
        chain, _ = _build_filter_chain(
            enable_smooth=True, caps=_caps_30cap(), video=_video_60fps_hd(),
        )
    assert chain.stages[0] is fake_rife
    ctor.assert_called_once_with(
        source_fps=24.0, target_fps=30, width=1920, height=1080,
    )


# ---- _handle_cast extension: reads enable_smooth, returns info_message -----


def test_handle_cast_passes_enable_smooth_through_to_filter_chain():
    """enable_smooth=True + happy probe → handler constructs Transcoder with
    a RIFE-bearing FilterChain (verified via the FilterChain arg captured on
    the Transcoder call)."""
    from castbooster.transcoder import TranscoderState as _TS

    class _FakeTranscoder:
        def __init__(self, *, filter_chain, **kw):
            self._state = _TS.IDLE
            self._filter_chain = filter_chain
            self._output_dir = kw.get("base_output_dir", Path("/tmp/fake")) / "v1"

        @property
        def state(self): return self._state
        @property
        def idle_reason(self): return None
        @property
        def output_dir(self): return self._output_dir

        def start(self):
            self._state = _TS.WARMING

        def wait_until_ready(self, timeout=None):
            self._state = _TS.READY
            return True

        def stop(self, drain_seconds=2.0):
            self._state = _TS.TERMINATED

    captured = {}

    def _ctor(*args, **kwargs):
        captured["chain"] = kwargs["filter_chain"]
        return _FakeTranscoder(**kwargs)

    fake_rife = MagicMock(name="RIFEFilter")
    fake_rife.pipeline_spec.return_value = MagicMock()
    fake_rife.render.return_value = "null"

    async def _go():
        app = _build_test_app()
        sess = app["session_store"].create("https://example.com/p.m3u8")
        cast_uuid = "12345678-1234-5678-1234-567812345678"
        with patch("castbooster.proxy.Transcoder", _ctor), \
             patch("castbooster.proxy.license.is_pro", return_value=True), \
             patch("castbooster.proxy.ffmpeg_probe.probe_input_video",
                   return_value=_video_60fps_hd()), \
             patch("castbooster.proxy.RIFEFilter", return_value=fake_rife):
            resp = await _handle_cast(
                app,
                {"token": sess.token, "castUuid": cast_uuid,
                 "enable_smooth": True},
            )
        assert resp["status"] == "ok"
        assert captured["chain"].stages[0] is fake_rife

    _run(_go())


def test_handle_cast_enable_smooth_false_constructs_noop_chain():
    from castbooster.transcoder import TranscoderState as _TS

    captured = {}

    class _FakeTranscoder:
        def __init__(self, *, filter_chain, **kw):
            captured["chain"] = filter_chain
            self._state = _TS.IDLE
            self._output_dir = kw.get("base_output_dir", Path("/tmp/fake")) / "v1"
        @property
        def state(self): return self._state
        @property
        def idle_reason(self): return None
        @property
        def output_dir(self): return self._output_dir
        def start(self): self._state = _TS.WARMING
        def wait_until_ready(self, timeout=None):
            self._state = _TS.READY
            return True
        def stop(self, drain_seconds=2.0): self._state = _TS.TERMINATED

    async def _go():
        app = _build_test_app()
        sess = app["session_store"].create("https://example.com/p.m3u8")
        cast_uuid = "12345678-1234-5678-1234-567812345678"
        with patch("castbooster.proxy.Transcoder", _FakeTranscoder):
            resp = await _handle_cast(
                app,
                {"token": sess.token, "castUuid": cast_uuid,
                 "enable_smooth": False},
            )
        assert resp["status"] == "ok"
        assert isinstance(captured["chain"].stages[0], NoopFilter)

    _run(_go())


def test_handle_cast_enable_smooth_true_without_pro_rejected():
    """Defense-in-depth: proxy refuses enable_smooth=True if !is_pro()."""
    async def _go():
        app = _build_test_app()
        sess = app["session_store"].create("https://example.com/p.m3u8")
        cast_uuid = "12345678-1234-5678-1234-567812345678"
        with patch("castbooster.proxy.license.is_pro", return_value=False):
            resp = await _handle_cast(
                app,
                {"token": sess.token, "castUuid": cast_uuid,
                 "enable_smooth": True},
            )
        assert resp["status"] == "error"
        assert "pro" in resp["detail"].lower()
    _run(_go())


# ---- _handle_set_filter_chain (mid-cast hot-reload) ------------------------


def _seed_session_with_transcoder(app, *, smooth_was_on: bool = False):
    """Helper: create a session, attach a mock Transcoder, cache caps+probed
    video so set_filter_chain doesn't have to re-probe.

    The mock transcoder.set_filter_chain returns True by default — tests
    flip it via attribute assignment when they want the failure path.
    """
    sess = app["session_store"].create("https://example.com/p.m3u8")
    sess.cast_uuid = "12345678-1234-5678-1234-567812345678"
    sess.probed_video = _video_60fps_hd()

    tx = MagicMock(name="Transcoder")
    tx.state = TranscoderState.READY
    tx.set_filter_chain.return_value = True
    tx.has_active_pipeline.return_value = smooth_was_on
    sess.transcoder = tx
    return sess, tx


def test_set_filter_chain_happy_path_enable_smooth_true():
    fake_rife = MagicMock(name="RIFEFilter")
    fake_rife.pipeline_spec.return_value = MagicMock()
    fake_rife.render.return_value = "null"

    async def _go():
        app = _build_test_app()
        sess, tx = _seed_session_with_transcoder(app, smooth_was_on=False)

        def _flip(chain):
            tx.has_active_pipeline.return_value = not isinstance(
                chain.stages[0], NoopFilter
            )
            return True
        tx.set_filter_chain.side_effect = _flip

        with patch("castbooster.proxy.license.is_pro", return_value=True), \
             patch("castbooster.proxy.RIFEFilter", return_value=fake_rife):
            resp = await _handle_set_filter_chain(
                app, {"token": sess.token, "enable_smooth": True},
            )
        assert resp["type"] == "filter_chain_set"
        assert resp["status"] == "ok"
        assert resp["enable_smooth_actual"] is True
        tx.set_filter_chain.assert_called_once()
    _run(_go())


def test_set_filter_chain_happy_path_enable_smooth_false():
    async def _go():
        app = _build_test_app()
        sess, tx = _seed_session_with_transcoder(app, smooth_was_on=True)

        def _flip(chain):
            tx.has_active_pipeline.return_value = not isinstance(
                chain.stages[0], NoopFilter
            )
            return True
        tx.set_filter_chain.side_effect = _flip

        resp = await _handle_set_filter_chain(
            app, {"token": sess.token, "enable_smooth": False},
        )
        assert resp["status"] == "ok"
        assert resp["enable_smooth_actual"] is False
        call = tx.set_filter_chain.call_args
        assert isinstance(call.args[0].stages[0], NoopFilter)
    _run(_go())


def test_set_filter_chain_failure_queues_warning_and_keeps_old_chain():
    """When transcoder.set_filter_chain returns False, the OLD chain keeps
    streaming. Handler queues a popup_warning and returns status=failed."""
    fake_rife = MagicMock(name="RIFEFilter")
    fake_rife.pipeline_spec.return_value = MagicMock()
    fake_rife.render.return_value = "null"

    async def _go():
        app = _build_test_app()
        sess, tx = _seed_session_with_transcoder(app, smooth_was_on=False)
        tx.set_filter_chain.return_value = False
        # has_active_pipeline still reflects the OLD chain (no RIFE).

        with patch("castbooster.proxy.license.is_pro", return_value=True), \
             patch("castbooster.proxy.RIFEFilter", return_value=fake_rife):
            resp = await _handle_set_filter_chain(
                app, {"token": sess.token, "enable_smooth": True},
            )
        assert resp["status"] == "failed"
        assert resp["enable_smooth_actual"] is False
        assert "warning" in resp
        assert sess.pending_warnings  # at least one warning queued
        assert any("smoothness" in w.lower() for w in sess.pending_warnings)
    _run(_go())


def test_set_filter_chain_raises_runtime_error_returns_failed():
    """If transcoder.set_filter_chain raises (e.g., wrong state), handler
    catches and returns a 'failed' response with a warning."""
    async def _go():
        app = _build_test_app()
        sess, tx = _seed_session_with_transcoder(app, smooth_was_on=False)
        tx.set_filter_chain.side_effect = RuntimeError(
            "set_filter_chain in state WARMING; only READY/STREAMING/STALLED"
        )

        resp = await _handle_set_filter_chain(
            app, {"token": sess.token, "enable_smooth": False},
        )
        assert resp["status"] == "failed"
        assert "warning" in resp
    _run(_go())


def test_set_filter_chain_rejects_smooth_without_pro():
    async def _go():
        app = _build_test_app()
        sess, _tx = _seed_session_with_transcoder(app, smooth_was_on=False)
        with patch("castbooster.proxy.license.is_pro", return_value=False):
            resp = await _handle_set_filter_chain(
                app, {"token": sess.token, "enable_smooth": True},
            )
        assert resp["status"] == "error"
        assert "pro" in resp["detail"].lower()
    _run(_go())


def test_set_filter_chain_unknown_token():
    async def _go():
        app = _build_test_app()
        resp = await _handle_set_filter_chain(
            app, {"token": "unknown", "enable_smooth": True},
        )
        assert resp["status"] == "error"
        assert "token" in resp["detail"].lower() or "session" in resp["detail"].lower()
    _run(_go())


def test_set_filter_chain_no_active_transcoder():
    """Passthrough-only session has no transcoder → handler returns error."""
    async def _go():
        app = _build_test_app()
        sess = app["session_store"].create("https://example.com/p.m3u8")
        sess.passthrough_only = True  # transcoder stays None
        resp = await _handle_set_filter_chain(
            app, {"token": sess.token, "enable_smooth": True},
        )
        assert resp["status"] == "error"
    _run(_go())


# ---- _handle_get_session_status (popup polling endpoint) -------------------


def test_get_session_status_returns_state_and_drains_warnings():
    async def _go():
        app = _build_test_app()
        sess, tx = _seed_session_with_transcoder(app, smooth_was_on=True)
        sess.pending_warnings.append("Smoothness stopped — continuing without")
        sess.pending_warnings.append("Another warning")

        resp = await _handle_get_session_status(
            app, {"token": sess.token},
        )
        assert resp["type"] == "session_status"
        assert resp["state"] == "ready"
        assert resp["enable_smooth_actual"] is True
        assert resp["warnings"] == [
            "Smoothness stopped — continuing without",
            "Another warning",
        ]
        # Queue is drained — second call returns empty.
        resp2 = await _handle_get_session_status(
            app, {"token": sess.token},
        )
        assert resp2["warnings"] == []
    _run(_go())


def test_get_session_status_no_transcoder_returns_no_session():
    async def _go():
        app = _build_test_app()
        sess = app["session_store"].create("https://example.com/p.m3u8")
        # No transcoder attached.
        resp = await _handle_get_session_status(
            app, {"token": sess.token},
        )
        assert resp["type"] == "session_status"
        assert resp["state"] == "no_session"
        assert resp["enable_smooth_actual"] is False
        assert resp["warnings"] == []
    _run(_go())


def test_get_session_status_unknown_token():
    async def _go():
        app = _build_test_app()
        resp = await _handle_get_session_status(
            app, {"token": "unknown"},
        )
        assert resp["state"] == "no_session"
        assert resp["enable_smooth_actual"] is False
    _run(_go())


# ---- _watchdog_tick (failure-detection demote) -----------------------------


def test_watchdog_demotes_failed_session_with_active_pipeline():
    """When a transcoder is FAILED and was running RIFE, the watchdog
    initiates a demote-to-NoopFilter and queues a popup warning."""
    async def _go():
        app = _build_test_app()
        sess, tx = _seed_session_with_transcoder(app, smooth_was_on=True)
        tx.state = TranscoderState.FAILED
        # has_active_pipeline returns True (RIFE was active when it died)
        tx.has_active_pipeline.return_value = True
        tx.last_reload_error = None

        await _watchdog_tick(app)

        # Watchdog called set_filter_chain with a NoopFilter chain
        tx.set_filter_chain.assert_called_once()
        called_chain = tx.set_filter_chain.call_args.args[0]
        assert isinstance(called_chain.stages[0], NoopFilter)
        # Warning queued for the popup to pick up next poll
        assert sess.pending_warnings
    _run(_go())


def test_watchdog_skips_healthy_sessions():
    async def _go():
        app = _build_test_app()
        sess, tx = _seed_session_with_transcoder(app, smooth_was_on=True)
        tx.state = TranscoderState.STREAMING  # healthy
        tx.has_active_pipeline.return_value = True

        await _watchdog_tick(app)

        tx.set_filter_chain.assert_not_called()
        assert not sess.pending_warnings
    _run(_go())


def test_watchdog_skips_failed_session_without_active_pipeline():
    """If the FAILED session was already on NoopFilter (no pipeline active),
    there's nothing to demote — leave the user-facing 'cast failed' UX to
    the existing _handle_cast fall-back path."""
    async def _go():
        app = _build_test_app()
        sess, tx = _seed_session_with_transcoder(app, smooth_was_on=False)
        tx.state = TranscoderState.FAILED
        tx.has_active_pipeline.return_value = False

        await _watchdog_tick(app)

        tx.set_filter_chain.assert_not_called()
        assert not sess.pending_warnings
    _run(_go())


def test_watchdog_does_not_re_demote_already_demoted_session():
    """Idempotency: once we've demoted, has_active_pipeline returns False
    on subsequent ticks so the watchdog skips the session."""
    async def _go():
        app = _build_test_app()
        sess, tx = _seed_session_with_transcoder(app, smooth_was_on=True)
        tx.state = TranscoderState.FAILED
        # First tick: pipeline was active → demote
        tx.has_active_pipeline.return_value = True

        def _after_demote(chain):
            tx.has_active_pipeline.return_value = False
            return True
        tx.set_filter_chain.side_effect = _after_demote

        await _watchdog_tick(app)
        assert tx.set_filter_chain.call_count == 1

        # Second tick: pipeline now inactive → no-op
        await _watchdog_tick(app)
        assert tx.set_filter_chain.call_count == 1
    _run(_go())


def test_handle_cast_vfr_source_surfaces_info_message():
    from castbooster.transcoder import TranscoderState as _TS

    class _FakeTranscoder:
        def __init__(self, **kw):
            self._state = _TS.IDLE
            self._output_dir = kw.get("base_output_dir", Path("/tmp/fake")) / "v1"
        @property
        def state(self): return self._state
        @property
        def idle_reason(self): return None
        @property
        def output_dir(self): return self._output_dir
        def start(self): self._state = _TS.WARMING
        def wait_until_ready(self, timeout=None):
            self._state = _TS.READY
            return True
        def stop(self, drain_seconds=2.0): self._state = _TS.TERMINATED

    async def _go():
        app = _build_test_app()
        sess = app["session_store"].create("https://example.com/p.m3u8")
        cast_uuid = "12345678-1234-5678-1234-567812345678"
        with patch("castbooster.proxy.Transcoder", _FakeTranscoder), \
             patch("castbooster.proxy.license.is_pro", return_value=True), \
             patch("castbooster.proxy.ffmpeg_probe.probe_input_video",
                   return_value=_video_vfr()):
            resp = await _handle_cast(
                app,
                {"token": sess.token, "castUuid": cast_uuid,
                 "enable_smooth": True},
            )
        assert resp["status"] == "ok"
        assert "info_message" in resp
        assert "frame rate" in resp["info_message"].lower()

    _run(_go())


def test_build_filter_chain_returns_probe_failure_info_when_video_is_None_and_smooth_requested():
    """Pillar 3.5: when user toggled smooth ON but ffprobe failed,
    surface a visible info_message instead of silently demoting."""
    from castbooster.proxy import _PROBE_FAILED_INFO, _build_filter_chain
    from castbooster.receiver_caps import capabilities_for_model

    caps = capabilities_for_model("Chromecast HD")  # not audio-only, max_fps=60
    chain, info = _build_filter_chain(
        enable_smooth=True, caps=caps, video=None,
    )
    # Still demotes to noop (we can't run RIFE without source FPS info)
    from castbooster.filter_chain import NoopFilter
    assert isinstance(chain.stages[0], NoopFilter)
    # But this time the user sees WHY
    assert info == _PROBE_FAILED_INFO


def test_build_filter_chain_stays_silent_when_video_None_but_smooth_off():
    """If the user didn't ask for smooth, we shouldn't bother them with a
    'probe failed' message — they wouldn't have gotten RIFE anyway."""
    from castbooster.proxy import _build_filter_chain
    from castbooster.receiver_caps import capabilities_for_model

    caps = capabilities_for_model("Chromecast HD")
    chain, info = _build_filter_chain(
        enable_smooth=False, caps=caps, video=None,
    )
    assert info is None


import logging


def test_handle_capabilities_logs_response_for_diagnostics(caplog):
    """Pillar 3.5 thread 2: capabilities response is now logged so we can
    see what Abed's machine actually reports (is_pro / vulkan_available)."""
    from castbooster.proxy import _handle_capabilities
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level(logging.INFO, logger="castbooster"):
            loop.run_until_complete(_handle_capabilities(None, {}))
    finally:
        loop.close()

    matches = [r for r in caplog.records
               if "capabilities:" in r.getMessage()
               and "is_pro=" in r.getMessage()
               and "vulkan_available=" in r.getMessage()]
    assert matches, f"expected capabilities log, got {[r.getMessage() for r in caplog.records]}"


def test_handle_cast_logs_raw_enable_smooth_value(caplog):
    """Pillar 3.5 thread 2: log raw msg.get('enable_smooth') BEFORE
    coercion so we see exactly what the wire delivered."""
    from castbooster.proxy import _handle_cast
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level(logging.INFO, logger="castbooster"):
            try:
                loop.run_until_complete(
                    _handle_cast(
                        {"session_store": None, "cast_manager": None},  # dummy app dict
                        {"token": "abc12345xx", "castUuid": "u", "enable_smooth": True},
                    )
                )
            except Exception:
                # OK if _handle_cast raises later — we only care about the log line
                pass
    finally:
        loop.close()

    matches = [r for r in caplog.records
               if "enable_smooth_raw=" in r.getMessage()]
    assert matches, f"expected enable_smooth_raw log, got {[r.getMessage() for r in caplog.records]}"
    assert "True" in matches[0].getMessage()
