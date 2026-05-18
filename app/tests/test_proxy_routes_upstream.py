"""End-to-end tests for the renamed /s/{token}/upstream/* routes.

Uses aiohttp's AppRunner + TestClient — connects from 127.0.0.1, so the
loopback gate passes. Non-loopback rejection is tested separately in
test_proxy_require_loopback (Task 3) at the unit level."""
import asyncio
import base64

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from castbooster.proxy import _build_app


def _run(coro):
    return asyncio.run(coro)


def test_upstream_master_route_resolves():
    """GET /s/{token}/upstream/master.m3u8 must resolve to a handler (not 404).
    We expect 404 on the SESSION lookup (no session was registered), NOT on the
    route match. This proves the route exists."""
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            resp = await c.get("/s/missingtoken/upstream/master.m3u8")
            assert resp.status == 404
            body = await resp.text()
            assert "unknown" in body.lower() or "expired" in body.lower()
    _run(_go())


def test_upstream_fetch_route_resolves():
    """Same for /s/{token}/upstream/fetch."""
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            u = base64.urlsafe_b64encode(b"https://example.com/x.ts").decode().rstrip("=")
            resp = await c.get(f"/s/missingtoken/upstream/fetch?u={u}")
            assert resp.status == 404
    _run(_go())


def test_options_preflight_on_upstream_master():
    """CORS preflight under the new path."""
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            resp = await c.options("/s/missingtoken/upstream/master.m3u8")
            assert resp.status == 204
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    _run(_go())


def test_old_master_route_is_gone():
    """The pre-P2.4 path /s/{token}/master.m3u8 must no longer resolve."""
    async def _go():
        app = _build_app("127.0.0.1")
        async with TestClient(TestServer(app)) as c:
            resp = await c.get("/s/missingtoken/master.m3u8")
            assert resp.status == 404
            # Distinguishes "route not registered" (aiohttp's 404 has no body or a
            # generic body) from "session not found" (our handler returns text)
            body = await resp.text()
            assert "unknown" not in body.lower() and "expired" not in body.lower()
    _run(_go())
