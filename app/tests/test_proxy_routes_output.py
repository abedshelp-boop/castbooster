"""Tests for the new /s/{token}/output/* routes.

A real Transcoder isn't spawned — we install a MagicMock that mimics
the Transcoder API surface (state property + output_dir property)
on a registered StreamSession's `transcoder` field.
"""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from castbooster.proxy import _build_app
from castbooster.transcoder import TranscoderState


def _run(coro):
    return asyncio.run(coro)


def _make_fake_transcoder(state: TranscoderState, output_dir: Path) -> MagicMock:
    fake = MagicMock()
    type(fake).state = property(lambda self: state)
    type(fake).output_dir = property(lambda self: output_dir)
    type(fake).idle_reason = property(lambda self: None)
    return fake


def test_output_master_503_when_transcoder_warming(tmp_path):
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            sess = app["session_store"].create("https://example.com/playlist.m3u8")
            sess.transcoder = _make_fake_transcoder(TranscoderState.WARMING, tmp_path / "v1")
            sess.output_dir = tmp_path
            resp = await c.get(f"/s/{sess.token}/output/master.m3u8")
            assert resp.status == 503
            body = await resp.json()
            assert body["state"] == "warming"
            assert "idle_reason" in body
    _run(_go())


def test_output_master_503_when_no_transcoder():
    """A session without a transcoder (e.g. before Task 7 lands) must also 503."""
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            sess = app["session_store"].create("https://example.com/playlist.m3u8")
            # sess.transcoder is None (default)
            resp = await c.get(f"/s/{sess.token}/output/master.m3u8")
            assert resp.status == 503
    _run(_go())


def test_output_master_200_when_ready(tmp_path):
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            sess = app["session_store"].create("https://example.com/playlist.m3u8")
            slot_dir = tmp_path / "v1"
            slot_dir.mkdir(parents=True)
            master = slot_dir / "master.m3u8"
            master.write_text(
                "#EXTM3U\n#EXT-X-VERSION:3\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720\n"
                "variant.m3u8\n",
                encoding="utf-8",
            )
            sess.transcoder = _make_fake_transcoder(TranscoderState.READY, slot_dir)
            sess.output_dir = tmp_path
            resp = await c.get(f"/s/{sess.token}/output/master.m3u8")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("application/vnd.apple.mpegurl")
            assert resp.headers["Access-Control-Allow-Origin"] == "*"
            body = await resp.text()
            assert body.startswith("#EXTM3U")
    _run(_go())


def test_output_master_200_when_reloading(tmp_path):
    """CRITICAL: RELOADING is in the alive predicate. Direct application
    of the 2026-04-22 IDLE-race lesson to P2.3's new state."""
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            sess = app["session_store"].create("https://example.com/playlist.m3u8")
            slot_dir = tmp_path / "v1"
            slot_dir.mkdir(parents=True)
            (slot_dir / "master.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
            sess.transcoder = _make_fake_transcoder(TranscoderState.RELOADING, slot_dir)
            sess.output_dir = tmp_path
            resp = await c.get(f"/s/{sess.token}/output/master.m3u8")
            assert resp.status == 200, (
                "RELOADING must be treated as alive — see spec §3.1 alive predicate"
            )
    _run(_go())


def test_output_master_200_when_streaming(tmp_path):
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            sess = app["session_store"].create("https://example.com/playlist.m3u8")
            slot_dir = tmp_path / "v1"
            slot_dir.mkdir(parents=True)
            (slot_dir / "master.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
            sess.transcoder = _make_fake_transcoder(TranscoderState.STREAMING, slot_dir)
            sess.output_dir = tmp_path
            resp = await c.get(f"/s/{sess.token}/output/master.m3u8")
            assert resp.status == 200
    _run(_go())


def test_output_master_200_when_stalled(tmp_path):
    """STALLED is alive — we serve what we have. P2.2 design."""
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            sess = app["session_store"].create("https://example.com/playlist.m3u8")
            slot_dir = tmp_path / "v1"
            slot_dir.mkdir(parents=True)
            (slot_dir / "master.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
            sess.transcoder = _make_fake_transcoder(TranscoderState.STALLED, slot_dir)
            sess.output_dir = tmp_path
            resp = await c.get(f"/s/{sess.token}/output/master.m3u8")
            assert resp.status == 200
    _run(_go())


def test_output_master_503_when_failed(tmp_path):
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            sess = app["session_store"].create("https://example.com/playlist.m3u8")
            sess.transcoder = _make_fake_transcoder(TranscoderState.FAILED, tmp_path / "v1")
            sess.output_dir = tmp_path
            resp = await c.get(f"/s/{sess.token}/output/master.m3u8")
            assert resp.status == 503
    _run(_go())


def test_output_segment_serves_disk_file(tmp_path):
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            sess = app["session_store"].create("https://example.com/playlist.m3u8")
            slot_dir = tmp_path / "v1"
            slot_dir.mkdir(parents=True)
            payload = b"\x00\x01\x02FAKETS" * 100
            (slot_dir / "seg_00001.ts").write_bytes(payload)
            sess.transcoder = _make_fake_transcoder(TranscoderState.STREAMING, slot_dir)
            sess.output_dir = tmp_path
            resp = await c.get(f"/s/{sess.token}/output/seg_00001.ts")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "video/mp2t"
            body = await resp.read()
            assert body == payload
    _run(_go())


def test_output_unknown_token_returns_404():
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            resp = await c.get("/s/nonexistent/output/master.m3u8")
            assert resp.status == 404
    _run(_go())


def test_upstream_and_output_no_collision(tmp_path):
    """Verifies aiohttp's literal-segment-first matching disambiguates
    /upstream/master.m3u8 vs /output/master.m3u8 under the same token."""
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            sess = app["session_store"].create("https://example.com/playlist.m3u8")
            slot_dir = tmp_path / "v1"
            slot_dir.mkdir(parents=True)
            (slot_dir / "master.m3u8").write_text("#EXTM3U\nfake-output\n", encoding="utf-8")
            sess.transcoder = _make_fake_transcoder(TranscoderState.READY, slot_dir)
            sess.output_dir = tmp_path
            # /output/ — served from disk (200, our fake-output body).
            r_out = await c.get(f"/s/{sess.token}/output/master.m3u8")
            assert r_out.status == 200
            body_out = await r_out.text()
            assert "fake-output" in body_out
            # /upstream/ — session has no real upstream fetch wired (the example.com
            # URL would 502 on a real network), but routing must reach the handler.
            # Test client connects from 127.0.0.1 so loopback gate passes.
            r_up = await c.get(f"/s/{sess.token}/upstream/master.m3u8")
            # Either 502 (failed upstream fetch) or 200 — what matters is it's NOT
            # the /output/ body and NOT a routing 404.
            assert r_up.status != 404
            if r_up.status == 200:
                body_up = await r_up.text()
                assert "fake-output" not in body_up
    _run(_go())
