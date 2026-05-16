"""Cleanup wiring tests for CastManager.on_session_end + failure counter
+ play-during-play race. Mocks pychromecast so we don't need a real
Chromecast on the network.
"""
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from castbooster.caster import CastManager


def _make_manager_with_fake_connection(uuid: UUID, friendly_name: str = "Test TV"):
    """Build a CastManager pre-populated with one discovered + connected device.

    Returns (manager, fake_chromecast). The fake Chromecast has a media_controller
    with mock play_media / block_until_active / stop / pause / play / seek.
    """
    cm = CastManager()
    info = MagicMock()
    info.friendly_name = friendly_name
    info.model_name = "Chromecast"
    info.host = "192.168.1.50"
    cm._casts[uuid] = info
    fake_cast = MagicMock()
    fake_cast.media_controller = MagicMock()
    cm._connections[uuid] = fake_cast
    return cm, fake_cast


def test_on_session_end_callback_stops_transcoder_on_user_stop():
    uuid = UUID("12345678-1234-5678-1234-567812345678")
    cm, fake_cast = _make_manager_with_fake_connection(uuid)
    cb = MagicMock()
    # Simulate a successful play() with on_session_end registered.
    with cm._lock:
        cm._session_end_callbacks[uuid] = cb
        cm._wakelock_held.add(uuid)
    # User-stop path: cm.control('stop') → mc.stop() → _release_wakelock_for
    with patch("castbooster.caster.wakelock"):
        cm.control(str(uuid), "stop")
    cb.assert_called_once()


def test_on_session_end_callback_stops_transcoder_on_natural_end():
    """When _MediaStatusLogger sees IDLE/FINISHED after a prior PLAYING,
    it must fan out through the on_idle callback we registered in play()."""
    uuid = UUID("12345678-1234-5678-1234-567812345679")
    cm, _ = _make_manager_with_fake_connection(uuid)
    cb = MagicMock()
    with cm._lock:
        cm._session_end_callbacks[uuid] = cb
        cm._wakelock_held.add(uuid)
    # The natural-end path: _MediaStatusLogger fires on_idle which calls
    # _release_wakelock_for(uuid).
    with patch("castbooster.caster.wakelock"):
        cm._release_wakelock_for(uuid)
    cb.assert_called_once()


def test_on_session_end_callback_stops_transcoder_on_device_remove():
    uuid = UUID("12345678-1234-5678-1234-56781234567a")
    cm, _ = _make_manager_with_fake_connection(uuid)
    cb = MagicMock()
    with cm._lock:
        cm._session_end_callbacks[uuid] = cb
        cm._wakelock_held.add(uuid)
    # _on_remove(uuid, _service, _cast_info) → _release_wakelock_for
    with patch("castbooster.caster.wakelock"):
        cm._on_remove(uuid, "_googlecast._tcp.local.", None)
    cb.assert_called_once()


def test_play_twice_on_same_uuid_fires_previous_callback():
    """Play-during-play race (§3.4): second play() overwrites the callback;
    previous callback MUST be invoked so its transcoder doesn't leak."""
    uuid = UUID("12345678-1234-5678-1234-56781234567b")
    cm, fake_cast = _make_manager_with_fake_connection(uuid)
    cb_a = MagicMock(name="cb_a")
    cb_b = MagicMock(name="cb_b")
    # First play registers cb_a.
    with cm._lock:
        cm._session_end_callbacks[uuid] = cb_a
    # Second play with cb_b: cm.play() implementation must fire cb_a
    # before stashing cb_b (the test simulates the overwrite-detection path
    # directly by invoking the private helper).
    cm._register_session_end_callback(uuid, cb_b)
    cb_a.assert_called_once()
    cb_b.assert_not_called()
    assert cm._session_end_callbacks[uuid] is cb_b


def test_transcoder_failure_stats_starts_at_zero():
    cm = CastManager()
    stats = cm.transcoder_failure_stats()
    assert stats == {"total": 0, "by_reason": {}}


def test_transcoder_failure_stats_records_reason():
    cm = CastManager()
    cm.record_transcoder_failure("warming_timed_out")
    cm.record_transcoder_failure("warming_timed_out")
    cm.record_transcoder_failure("encoder_init_failed")
    stats = cm.transcoder_failure_stats()
    assert stats == {
        "total": 3,
        "by_reason": {"warming_timed_out": 2, "encoder_init_failed": 1},
    }


def test_transcoder_failure_stats_returns_snapshot_not_alias():
    """The counter dict is internal state; the public accessor must return
    a deep copy so callers can't mutate it."""
    cm = CastManager()
    cm.record_transcoder_failure("warming_timed_out")
    snapshot = cm.transcoder_failure_stats()
    snapshot["by_reason"]["fake_reason"] = 99
    snapshot["total"] = 999
    second = cm.transcoder_failure_stats()
    assert second == {"total": 1, "by_reason": {"warming_timed_out": 1}}


def test_register_with_none_pops_without_firing():
    """Explicit None (clear path) must pop the existing callback WITHOUT firing
    it. Used by callers that want to clear a registration without triggering
    teardown (e.g. tray-menu reset).
    """
    uuid = UUID("12345678-1234-5678-1234-56781234567c")
    cm = CastManager()
    cb_a = MagicMock(name="cb_a")
    with cm._lock:
        cm._session_end_callbacks[uuid] = cb_a
    cm._register_session_end_callback(uuid, None)
    cb_a.assert_not_called()
    assert uuid not in cm._session_end_callbacks
