"""Tests for the WiFi-radio keepalive in CastManager.

The keepalive loop runs forever until cancelled and wants a real event loop.
Rather than pulling in pytest-asyncio, we drive it with asyncio.run() and
patch loop.sock_connect to record calls without doing any real I/O.

Two facets get covered:
1. The loop actually issues sock_connect calls at the configured cadence
   to the right (host, port) pair drawn from CastManager._casts.
2. Cancellation cleans up the task without raising into the test harness.
"""

from __future__ import annotations

import asyncio
import socket
import time
from unittest.mock import patch
from uuid import UUID

from castbooster.caster import CastManager


_TEST_UUID = UUID("00000000-0000-0000-0000-0000000000aa")
_TEST_HOST = "127.0.0.1"
_TEST_PORT = 8009  # Must match the hardcoded port in _keepalive_loop


class _FakeCastInfo:
    """Minimal stand-in for pychromecast.models.CastInfo. Only `host` is
    read by _keepalive_loop, but we expose friendly_name/model_name too
    in case the test grows."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.friendly_name = "test-cast"
        self.model_name = "Chromecast"


def _run_keepalive_for(seconds: float):
    """Spin up a CastManager, attach the running loop, prime _casts, and
    let _keepalive_loop run for `seconds` with sock_connect mocked. Returns
    the recorded list of (host, port) tuples it tried to connect to."""
    recorded: list = []

    async def _go():
        loop = asyncio.get_running_loop()
        cm = CastManager()
        cm.attach_loop(loop)
        cm._casts[_TEST_UUID] = _FakeCastInfo(_TEST_HOST)

        async def fake_connect(sock, addr):
            # Record and return immediately — no real network traffic.
            recorded.append(addr)
            return None

        # Patch the BOUND method on this specific loop. Don't touch socket()
        # itself: _keepalive_loop creates a real socket so we exercise the
        # SO_LINGER setsockopt path even in tests.
        with patch.object(loop, "sock_connect", side_effect=fake_connect):
            task = asyncio.create_task(cm._keepalive_loop(_TEST_UUID))
            try:
                await asyncio.sleep(seconds)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(_go())
    return recorded


def test_keepalive_connects_to_chromecast_host_and_port():
    # 3 seconds at 1.2s cadence (±0.1s jitter) should yield 2-3 connects.
    recorded = _run_keepalive_for(3.0)
    assert len(recorded) >= 1, f"expected ≥1 connect attempt, got {len(recorded)}"
    assert all(addr == (_TEST_HOST, _TEST_PORT) for addr in recorded), recorded


def test_keepalive_cancels_cleanly():
    # If cancellation didn't propagate, asyncio.run would warn or hang.
    # Just ensure the helper returns without raising.
    _run_keepalive_for(0.5)


def test_keepalive_skips_iteration_when_host_missing():
    """If the device has been removed from _casts, the loop should sleep
    and retry rather than crash. We expect 0 connect calls."""
    recorded: list = []

    async def _go():
        loop = asyncio.get_running_loop()
        cm = CastManager()
        cm.attach_loop(loop)
        # Deliberately don't populate _casts.

        async def fake_connect(sock, addr):
            recorded.append(addr)
            return None

        with patch.object(loop, "sock_connect", side_effect=fake_connect):
            task = asyncio.create_task(cm._keepalive_loop(_TEST_UUID))
            try:
                await asyncio.sleep(2.0)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(_go())
    assert recorded == []


def test_keepalive_creates_real_socket_with_so_linger():
    """The keepalive constructs a real socket each iteration so that
    SO_LINGER (which only takes effect on close) is in place. We sniff
    the socket constructor to assert one is created per iteration."""
    sockets_created: list = []
    real_socket = socket.socket

    def tracking_socket(*args, **kwargs):
        s = real_socket(*args, **kwargs)
        sockets_created.append(s)
        return s

    async def _go():
        loop = asyncio.get_running_loop()
        cm = CastManager()
        cm.attach_loop(loop)
        cm._casts[_TEST_UUID] = _FakeCastInfo(_TEST_HOST)

        async def fake_connect(sock, addr):
            return None

        with patch.object(loop, "sock_connect", side_effect=fake_connect), \
             patch("castbooster.caster.socket.socket", side_effect=tracking_socket):
            task = asyncio.create_task(cm._keepalive_loop(_TEST_UUID))
            try:
                await asyncio.sleep(2.0)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(_go())
    assert len(sockets_created) >= 1
    # Sockets are closed in the loop's `finally`, so they should be gone now.
    # `fileno()` returns -1 on closed sockets.
    for s in sockets_created:
        assert s.fileno() == -1, "keepalive must close every socket it opens"
