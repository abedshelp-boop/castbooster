"""Tests for CastManager.capabilities() — reads model_name from the
discovered cast info and maps it to ReceiverCaps."""
from unittest.mock import MagicMock
from uuid import UUID

from castbooster.caster import CastManager


def _make_cm_with_info(uuid: UUID, model_name: str) -> CastManager:
    cm = CastManager()
    info = MagicMock()
    info.friendly_name = "Test TV"
    info.model_name = model_name
    info.host = "192.168.1.50"
    cm._casts[uuid] = info
    return cm


def test_capabilities_for_chromecast_ultra():
    uuid = UUID("11111111-1111-1111-1111-111111111111")
    cm = _make_cm_with_info(uuid, "Chromecast Ultra")
    caps = cm.capabilities(str(uuid))
    assert caps.tier == "ultra"
    assert caps.max_height == 2160


def test_capabilities_for_plain_chromecast_optimistic():
    # Pillar 3.5: "Chromecast" model_name now defaults to 60fps (3rd-gen modal).
    uuid = UUID("22222222-2222-2222-2222-222222222222")
    cm = _make_cm_with_info(uuid, "Chromecast")
    caps = cm.capabilities(str(uuid))
    assert caps.tier == "3rd_gen_or_older"
    assert caps.max_fps == 60


def test_capabilities_for_unknown_uuid_returns_unknown_fallback():
    cm = CastManager()
    caps = cm.capabilities("00000000-0000-0000-0000-000000000000")
    assert caps.tier == "unknown"
    assert caps.max_width == 1920
    assert caps.audio_only is False


def test_capabilities_for_nest_hub_is_audio_only():
    uuid = UUID("33333333-3333-3333-3333-333333333333")
    cm = _make_cm_with_info(uuid, "Google Nest Hub")
    caps = cm.capabilities(str(uuid))
    assert caps.audio_only is True
