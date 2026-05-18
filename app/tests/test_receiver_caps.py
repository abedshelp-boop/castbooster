"""Tests for the static receiver-capabilities lookup."""
import pytest

from castbooster.receiver_caps import ReceiverCaps, capabilities_for_model


def test_chromecast_ultra_is_4k60_hdr_capable():
    caps = capabilities_for_model("Chromecast Ultra")
    assert caps.tier == "ultra"
    assert (caps.max_width, caps.max_height, caps.max_fps) == (3840, 2160, 60)
    assert caps.supports_h265 is True
    assert caps.supports_av1 is False
    assert caps.supports_ac3 is True
    assert caps.supports_eac3 is True
    assert caps.audio_only is False


def test_plain_chromecast_is_conservative_1080p30():
    # 1st/2nd/3rd gen Chromecasts all report model_name="Chromecast".
    # Conservative defaults: 1080p30 H.264 AAC only.
    caps = capabilities_for_model("Chromecast")
    assert caps.tier == "3rd_gen_or_older"
    assert (caps.max_width, caps.max_height, caps.max_fps) == (1920, 1080, 30)
    assert caps.supports_h265 is False
    assert caps.supports_ac3 is False


def test_chromecast_hd_is_gtv_1080p60():
    caps = capabilities_for_model("Chromecast HD")
    assert caps.tier == "gtv"
    assert caps.max_fps == 60
    assert caps.max_height == 1080
    assert caps.supports_av1 is True


def test_google_tv_4k_is_gtv_4k60_av1():
    caps = capabilities_for_model("Google TV")
    assert caps.tier == "gtv"
    assert caps.max_height == 2160
    assert caps.supports_av1 is True
    caps2 = capabilities_for_model("Chromecast 4K")
    assert caps2.tier == "gtv"
    assert caps2.max_height == 2160


def test_nest_hub_is_audio_only():
    for name in ("Google Nest Hub", "Google Home Hub", "Nest Hub Max"):
        caps = capabilities_for_model(name)
        assert caps.audio_only is True, name
        assert caps.tier == "audio_only"
        assert caps.max_width == 0


def test_nest_audio_devices_are_audio_only():
    for name in ("Google Home", "Nest Audio", "Google Home Mini", "Nest Mini"):
        caps = capabilities_for_model(name)
        assert caps.audio_only is True, name


def test_unknown_model_gets_conservative_fallback():
    for name in ("", "FutureCast 5000", "  ", "Roku"):
        caps = capabilities_for_model(name)
        assert caps.tier == "unknown"
        assert (caps.max_width, caps.max_height, caps.max_fps) == (1920, 1080, 30)
        assert caps.supports_h265 is False
        assert caps.audio_only is False


def test_receiver_caps_is_frozen():
    caps = capabilities_for_model("Chromecast Ultra")
    with pytest.raises((AttributeError, Exception)):
        caps.tier = "spoofed"
