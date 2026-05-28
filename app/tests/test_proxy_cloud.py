"""Tests for the P3.6 cloud branch added to _handle_cast.

Cloud-only-forever architecture lock (Abed 2026-05-28): smooth=True always
forks to cloud_cast; on success cm.play uses the cloud HLS URL and the
cloud on_session_end callback terminates the pod; on failure, sess
.passthrough_only flips and the existing passthrough URL is used.

Also covers the cloud-aware extensions in _handle_get_session_status
(cloud_state / cloud_warming_s / cloud_error fields for the popup warmup
card) and _handle_set_filter_chain (rejection mid-cloud-cast).

Mocks cloud_cast via unittest.mock.patch — no real RunPod calls.
"""
import asyncio
from unittest.mock import MagicMock, patch

from castbooster.cloud.cloud_cast import CloudCastResult
from castbooster.ffmpeg_probe import AccelProfile, InputVideoInfo
from castbooster.proxy import (
    _build_app,
    _build_source_headers_for_cloud,
    _handle_cast,
    _handle_get_session_status,
    _handle_set_filter_chain,
)
from castbooster.receiver_caps import ReceiverCaps


def _run(coro):
    return asyncio.run(coro)


def _video_24fps_hd():
    return InputVideoInfo(fps=24.0, width=1920, height=1080, pix_fmt="yuv420p")


def _caps_ultra_60():
    return ReceiverCaps(
        tier="ultra", max_width=3840, max_height=2160, max_fps=60,
        supports_h265=True, supports_av1=False,
        supports_ac3=True, supports_eac3=True, audio_only=False,
    )


def _build_test_app():
    """Mirrors test_proxy_smooth._build_test_app — fake AccelProfile + mocked
    CastManager so the cast handler can run offline."""
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


def _register_session(app, *, cookies=None, headers=None, ua=""):
    sess = app["session_store"].create(
        upstream_url="https://egydead.example/anime.m3u8",
        cookies=cookies or [
            {"name": "session", "value": "abc123", "domain": "egydead.example"},
        ],
        headers=headers or {},
        user_agent=ua or "TestUA/1.0",
    )
    return sess


# ---- _build_source_headers_for_cloud --------------------------------------


def test_build_source_headers_flattens_cookies_into_cookie_header():
    sess = MagicMock()
    sess.upstream_url = "https://egydead.example/anime.m3u8"
    sess.user_agent = "TestUA/1.0"
    sess.headers = {}
    sess.cookies = [
        {"name": "session", "value": "abc", "domain": "egydead.example"},
        {"name": "auth", "value": "xyz", "domain": "egydead.example"},
    ]
    h = _build_source_headers_for_cloud(sess)
    assert h["User-Agent"] == "TestUA/1.0"
    assert "session=abc" in h["Cookie"]
    assert "auth=xyz" in h["Cookie"]


def test_build_source_headers_excludes_cookies_for_other_hosts():
    sess = MagicMock()
    sess.upstream_url = "https://egydead.example/anime.m3u8"
    sess.user_agent = "TestUA"
    sess.headers = {}
    sess.cookies = [
        {"name": "session", "value": "abc", "domain": "egydead.example"},
        {"name": "stale", "value": "zzz", "domain": "other.com"},
    ]
    h = _build_source_headers_for_cloud(sess)
    assert "session=abc" in h["Cookie"]
    assert "stale" not in h.get("Cookie", "")


def test_build_source_headers_passes_through_custom_headers():
    sess = MagicMock()
    sess.upstream_url = "https://egydead.example/anime.m3u8"
    sess.user_agent = "UA"
    sess.headers = {"Referer": "https://egydead.example/ep1.html"}
    sess.cookies = []
    h = _build_source_headers_for_cloud(sess)
    assert h["Referer"] == "https://egydead.example/ep1.html"


def test_build_source_headers_no_cookies_no_cookie_header():
    """When sess has no cookies for the upstream host, no Cookie header is
    emitted — keeps the dict compact for cloud-worker validation."""
    sess = MagicMock()
    sess.upstream_url = "https://example.com/anime.m3u8"
    sess.user_agent = "UA"
    sess.headers = {}
    sess.cookies = []
    h = _build_source_headers_for_cloud(sess)
    assert "Cookie" not in h


# ---- _handle_cast cloud branch --------------------------------------------


def test_handle_cast_cloud_success_uses_cloud_url():
    """smooth=True + cloud success → cm.play is called with the cloud HLS
    URL (not the local /output URL) and the response carries the same."""
    async def _go():
        app = _build_test_app()
        sess = _register_session(app)

        fake_result = CloudCastResult(
            ok=True,
            hls_url="https://pod123-8080.proxy.runpod.net/hls/playlist.m3u8",
            pod_id="pod123",
            warming_status="streaming",
        )
        with patch("castbooster.proxy.license.is_pro", return_value=True), \
             patch("castbooster.proxy.license.vulkan_available", return_value=True), \
             patch("castbooster.proxy.ffmpeg_probe.probe_input_video",
                   return_value=_video_24fps_hd()), \
             patch("castbooster.cloud.cloud_cast.cloud_cast",
                   return_value=fake_result):
            resp = await _handle_cast(app, {
                "type": "cast",
                "token": sess.token,
                "castUuid": "uuid1",
                "enable_smooth": True,
            })
        assert resp["status"] == "ok"
        assert resp["playbackUrl"] == (
            "https://pod123-8080.proxy.runpod.net/hls/playlist.m3u8"
        )
        cm = app["cast_manager"]
        # cm.play called with the cloud URL + HLS content type
        assert cm.play.call_args.args[1] == (
            "https://pod123-8080.proxy.runpod.net/hls/playlist.m3u8"
        )
        assert cm.play.call_args.args[2] == "application/vnd.apple.mpegurl"
        # No local Transcoder was spawned
        assert sess.transcoder is None
        # Cloud state captured on the session
        assert sess.cloud_pod_id == "pod123"
        assert sess.cloud_hls_url == (
            "https://pod123-8080.proxy.runpod.net/hls/playlist.m3u8"
        )
        assert sess.cloud_warming_status == "streaming"
    _run(_go())


def test_handle_cast_cloud_failure_falls_to_passthrough():
    """smooth=True + cloud failure → passthrough URL + info_message + cloud
    error fields populated."""
    async def _go():
        app = _build_test_app()
        sess = _register_session(app)

        fake_result = CloudCastResult(
            ok=False,
            error="playlist never ready in 90s",
            warming_status="failed",
        )
        with patch("castbooster.proxy.license.is_pro", return_value=True), \
             patch("castbooster.proxy.license.vulkan_available", return_value=True), \
             patch("castbooster.proxy.ffmpeg_probe.probe_input_video",
                   return_value=_video_24fps_hd()), \
             patch("castbooster.cloud.cloud_cast.cloud_cast",
                   return_value=fake_result):
            resp = await _handle_cast(app, {
                "type": "cast",
                "token": sess.token,
                "castUuid": "uuid1",
                "enable_smooth": True,
            })
        assert resp["status"] == "ok"
        # Playback URL is the passthrough URL
        # (lan_ip:38123/s/<token>/upstream/master.m3u8)
        assert "/upstream/master.m3u8" in resp["playbackUrl"]
        # info_message surfaces the failure
        assert "Cloud cast failed" in resp.get("info_message", "")
        # sess fields capture the error
        assert sess.passthrough_only is True
        assert sess.cloud_pod_id is None
        assert sess.cloud_error == "playlist never ready in 90s"
        # No local Transcoder spawned on cloud failure either — passthrough
        # bypasses the transcoder entirely.
        assert sess.transcoder is None
    _run(_go())


def test_handle_cast_skips_cloud_when_smooth_false():
    """smooth=False → existing Noop+Transcoder path, NO cloud_cast call."""
    async def _go():
        app = _build_test_app()
        sess = _register_session(app)

        # Mock Transcoder to avoid real ffmpeg
        with patch("castbooster.proxy.license.is_pro", return_value=True), \
             patch("castbooster.proxy.ffmpeg_probe.probe_input_video",
                   return_value=_video_24fps_hd()), \
             patch("castbooster.proxy.Transcoder") as MockTranscoder, \
             patch("castbooster.cloud.cloud_cast.cloud_cast") as mock_cc:
            from castbooster.transcoder import TranscoderState
            fake_tc = MagicMock()
            fake_tc.start.return_value = None
            fake_tc.wait_until_ready.return_value = True
            fake_tc.state = TranscoderState.READY
            fake_tc.has_active_pipeline.return_value = False
            MockTranscoder.return_value = fake_tc
            resp = await _handle_cast(app, {
                "type": "cast",
                "token": sess.token,
                "castUuid": "uuid1",
                "enable_smooth": False,
            })
        assert resp["status"] == "ok"
        mock_cc.assert_not_called()
        assert sess.cloud_pod_id is None
    _run(_go())


def test_handle_cast_skips_cloud_when_not_pro():
    """enable_smooth=True + is_pro=False → rejects with 'pro required'.

    Cloud branch should NOT be reached because the existing pro gate is
    earlier in _handle_cast.
    """
    async def _go():
        app = _build_test_app()
        sess = _register_session(app)
        with patch("castbooster.proxy.license.is_pro", return_value=False), \
             patch("castbooster.cloud.cloud_cast.cloud_cast") as mock_cc:
            resp = await _handle_cast(app, {
                "type": "cast",
                "token": sess.token,
                "castUuid": "uuid1",
                "enable_smooth": True,
            })
        assert resp["status"] == "error"
        assert "pro" in resp["detail"].lower()
        mock_cc.assert_not_called()
    _run(_go())


def test_handle_cast_cloud_branch_uses_session_headers_for_cloud():
    """The cloud_cast call must receive source_url=sess.upstream_url and
    source_headers built from sess cookies/UA — proves the laptop → cloud
    header passthrough (Lock 3) is wired through _build_source_headers_for_cloud."""
    async def _go():
        app = _build_test_app()
        sess = _register_session(
            app,
            cookies=[
                {"name": "session", "value": "abc123",
                 "domain": "egydead.example"},
            ],
            headers={"Referer": "https://egydead.example/ep1.html"},
            ua="EgyDeadFan/9.9",
        )

        fake_result = CloudCastResult(
            ok=True,
            hls_url="https://pod-x.proxy.runpod.net/hls/playlist.m3u8",
            pod_id="pod-x",
            warming_status="streaming",
        )
        with patch("castbooster.proxy.license.is_pro", return_value=True), \
             patch("castbooster.proxy.ffmpeg_probe.probe_input_video",
                   return_value=_video_24fps_hd()), \
             patch("castbooster.cloud.cloud_cast.cloud_cast",
                   return_value=fake_result) as mock_cc:
            resp = await _handle_cast(app, {
                "type": "cast",
                "token": sess.token,
                "castUuid": "uuid1",
                "enable_smooth": True,
            })
        assert resp["status"] == "ok"
        # cloud_cast was called once
        assert mock_cc.call_count == 1
        kwargs = mock_cc.call_args.kwargs
        assert kwargs["source_url"] == "https://egydead.example/anime.m3u8"
        sh = kwargs["source_headers"]
        assert sh["User-Agent"] == "EgyDeadFan/9.9"
        assert sh["Referer"] == "https://egydead.example/ep1.html"
        assert "session=abc123" in sh["Cookie"]
    _run(_go())


# ---- _handle_get_session_status cloud fields ------------------------------


def test_get_session_status_cloud_in_flight():
    """When sess.cloud_pod_id is set and transcoder is None, the status
    response shape signals 'cloud' state + carries the warmup fields."""
    async def _go():
        app = _build_test_app()
        sess = _register_session(app)
        sess.cloud_pod_id = "pod123"
        sess.cloud_warming_status = "waiting_playlist"
        sess.cloud_error = ""
        resp = await _handle_get_session_status(app, {
            "type": "get_session_status",
            "token": sess.token,
        })
        assert resp["state"] == "cloud"
        assert resp["enable_smooth_actual"] is True
        assert resp["cloud_state"] == "waiting_playlist"
        assert resp["cloud_warming_s"] >= 0.0
        assert resp["cloud_error"] == ""
    _run(_go())


def test_get_session_status_no_session_includes_cloud_defaults():
    """Unknown token → no_session state + zero/empty cloud fields. The
    popup needs the keys present so the JS reading them doesn't crash."""
    async def _go():
        app = _build_test_app()
        resp = await _handle_get_session_status(app, {
            "type": "get_session_status",
            "token": "nope",
        })
        assert resp["state"] == "no_session"
        assert resp["cloud_state"] == ""
        assert resp["cloud_warming_s"] == 0.0
        assert resp["cloud_error"] == ""
    _run(_go())


# ---- _handle_set_filter_chain during cloud cast ---------------------------


def test_set_filter_chain_during_cloud_cast_rejects():
    """Mid-cloud-cast smooth toggle returns an error rather than silently
    accepting it. Phase 2 will support graceful re-spawn; MVP rejects."""
    async def _go():
        app = _build_test_app()
        sess = _register_session(app)
        sess.cloud_pod_id = "pod123"
        # No transcoder during cloud cast — the cloud-aware rejection
        # should fire BEFORE the existing no-transcoder error.
        with patch("castbooster.proxy.license.is_pro", return_value=True):
            resp = await _handle_set_filter_chain(app, {
                "type": "set_filter_chain",
                "token": sess.token,
                "enable_smooth": False,
            })
        assert resp["status"] == "error"
        assert "cloud cast" in resp["detail"]
    _run(_go())
