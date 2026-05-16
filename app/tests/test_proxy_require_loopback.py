"""Unit tests for the _require_loopback helper. Tested in isolation
so Task 5 (route wiring) can focus on assembly, not policy."""
from unittest.mock import MagicMock

from castbooster.proxy import _require_loopback


def test_require_loopback_accepts_ipv4_loopback():
    req = MagicMock()
    req.remote = "127.0.0.1"
    req.path = "/s/abc/upstream/master.m3u8"
    assert _require_loopback(req) is None


def test_require_loopback_accepts_ipv6_loopback():
    req = MagicMock()
    req.remote = "::1"
    req.path = "/s/abc/upstream/master.m3u8"
    assert _require_loopback(req) is None


def test_require_loopback_rejects_lan_ip():
    req = MagicMock()
    req.remote = "192.168.1.50"
    req.path = "/s/abc/upstream/master.m3u8"
    resp = _require_loopback(req)
    assert resp is not None
    assert resp.status == 403


def test_require_loopback_rejects_external_ip():
    req = MagicMock()
    req.remote = "1.2.3.4"
    req.path = "/s/abc/upstream/fetch"
    resp = _require_loopback(req)
    assert resp is not None
    assert resp.status == 403


def test_require_loopback_rejects_none_remote():
    """If aiohttp can't read peername (e.g. tunneled), request.remote can be None.
    Fail closed — reject."""
    req = MagicMock()
    req.remote = None
    req.path = "/s/abc/upstream/master.m3u8"
    resp = _require_loopback(req)
    assert resp is not None
    assert resp.status == 403
