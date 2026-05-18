"""Tests for the Chrome Native Messaging stdio host's app-liveness logic.

The bug being prevented (2026-05-17): nm_host's `_ensure_app_running()` was
treating any non-200 /health response — including transient timeouts during
an asyncio-loop freeze — as "app is dead" and immediately spawning a duplicate
detached process. In production we observed pychromecast SSL reconnect briefly
stall the proxy for ~3s; nm_host's 0.8s /health timeout fired during that
window and launched a duplicate, which then raced the original for port 38123
and lost the session state. The new `_app_status()` distinguishes "dead"
(connection refused — port not bound) from "busy" (timeout — port bound but
slow), and `_ensure_app_running()` retries once on "busy" before launching.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from castbooster import nm_host


# -------- _app_status() --------


def test_app_status_ok_when_health_returns_200(monkeypatch):
    fake_resp = MagicMock(status=200)
    fake_conn = MagicMock()
    fake_conn.getresponse.return_value = fake_resp
    monkeypatch.setattr(
        nm_host.http.client, "HTTPConnection", lambda *a, **k: fake_conn
    )
    assert nm_host._app_status() == "ok"


def test_app_status_dead_when_connection_refused(monkeypatch):
    def _refuse(*_a, **_k):
        raise ConnectionRefusedError("port not listening")

    monkeypatch.setattr(nm_host.http.client, "HTTPConnection", _refuse)
    assert nm_host._app_status() == "dead"


def test_app_status_busy_on_timeout(monkeypatch):
    fake_conn = MagicMock()
    fake_conn.request.side_effect = TimeoutError("slow")
    monkeypatch.setattr(
        nm_host.http.client, "HTTPConnection", lambda *a, **k: fake_conn
    )
    assert nm_host._app_status() == "busy"


def test_app_status_busy_on_non_200_response(monkeypatch):
    fake_resp = MagicMock(status=503)
    fake_conn = MagicMock()
    fake_conn.getresponse.return_value = fake_resp
    monkeypatch.setattr(
        nm_host.http.client, "HTTPConnection", lambda *a, **k: fake_conn
    )
    assert nm_host._app_status() == "busy"


# -------- _ensure_app_running() --------


def test_ensure_app_running_ok_does_not_launch(monkeypatch):
    fake_launch = MagicMock()
    monkeypatch.setattr(nm_host, "_launch_app_detached", fake_launch)
    monkeypatch.setattr(nm_host, "_app_status", lambda: "ok")
    assert nm_host._ensure_app_running() is True
    fake_launch.assert_not_called()


def test_ensure_app_running_busy_then_ok_does_not_launch(monkeypatch):
    """REGRESSION TEST for the duplicate-spawn bug.

    A transient /health timeout (busy) followed by a successful /health
    must NOT trigger a duplicate-process launch. Previously, nm_host would
    launch a duplicate on the very first busy response.
    """
    states = iter(["busy", "ok"])
    monkeypatch.setattr(nm_host, "_app_status", lambda: next(states))
    fake_launch = MagicMock()
    monkeypatch.setattr(nm_host, "_launch_app_detached", fake_launch)
    monkeypatch.setattr(nm_host.time, "sleep", lambda _x: None)
    assert nm_host._ensure_app_running() is True
    fake_launch.assert_not_called()


def test_ensure_app_running_dead_launches_immediately(monkeypatch):
    """Connection refused means port unbound — the app really is gone, so
    launch a new one without the retry delay."""
    # _app_status: first call returns "dead", subsequent post-launch polls
    # return "ok" (the new app came up).
    seq = iter(["dead", "ok"])
    monkeypatch.setattr(nm_host, "_app_status", lambda: next(seq))
    monkeypatch.setattr(nm_host, "_app_health_ok", lambda: True)
    fake_launch = MagicMock()
    monkeypatch.setattr(nm_host, "_launch_app_detached", fake_launch)
    monkeypatch.setattr(nm_host.time, "sleep", lambda _x: None)
    assert nm_host._ensure_app_running() is True
    fake_launch.assert_called_once()


def test_ensure_app_running_busy_then_still_busy_launches(monkeypatch):
    """If the app is busy on the initial check AND still busy after the
    retry delay, treat as unrecoverable and launch a duplicate. The retry
    is a grace window, not infinite patience."""
    seq = iter(["busy", "busy"])
    monkeypatch.setattr(nm_host, "_app_status", lambda: next(seq))
    monkeypatch.setattr(nm_host, "_app_health_ok", lambda: True)
    fake_launch = MagicMock()
    monkeypatch.setattr(nm_host, "_launch_app_detached", fake_launch)
    monkeypatch.setattr(nm_host.time, "sleep", lambda _x: None)
    assert nm_host._ensure_app_running() is True
    fake_launch.assert_called_once()
