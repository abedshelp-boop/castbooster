"""Static Chromecast receiver capability table.

pychromecast exposes `cast_info.model_name` as a string. We map it to a
frozen ReceiverCaps record describing what the device can play.

Today (P2.5) this is groundwork: the transcoder doesn't consume the caps
yet. P3 (RIFE @ 60fps) will be the first real consumer — it gates 60fps
output on `max_fps == 60`.

Conservative-fallback rule: unknown or ambiguous models get tier="unknown"
with safe 1080p30 H.264 AAC defaults. Notable ambiguity: pychromecast
returns model_name="Chromecast" for 1st/2nd/3rd gen indistinguishably, so
we treat all of them as 3rd_gen_or_older (1080p30) by default. 3rd-gen
owners with a 1080p60 TV pay a small smoothness cost — acceptable v1
tradeoff because the alternative is misclassifying a 1st gen and breaking
playback.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReceiverCaps:
    tier: str
    max_width: int
    max_height: int
    max_fps: int
    supports_h265: bool
    supports_av1: bool
    supports_ac3: bool
    supports_eac3: bool
    audio_only: bool


_UNKNOWN = ReceiverCaps(
    tier="unknown",
    max_width=1920, max_height=1080, max_fps=30,
    supports_h265=False, supports_av1=False,
    supports_ac3=False, supports_eac3=False,
    audio_only=False,
)

_AUDIO_ONLY = ReceiverCaps(
    tier="audio_only",
    max_width=0, max_height=0, max_fps=0,
    supports_h265=False, supports_av1=False,
    supports_ac3=False, supports_eac3=False,
    audio_only=True,
)

# Keys are exact model_name strings reported by pychromecast.
_TABLE: dict[str, ReceiverCaps] = {
    "Chromecast Ultra": ReceiverCaps(
        tier="ultra",
        max_width=3840, max_height=2160, max_fps=60,
        supports_h265=True, supports_av1=False,
        supports_ac3=True, supports_eac3=True,
        audio_only=False,
    ),
    "Chromecast": ReceiverCaps(
        tier="3rd_gen_or_older",
        max_width=1920, max_height=1080, max_fps=30,
        supports_h265=False, supports_av1=False,
        supports_ac3=False, supports_eac3=False,
        audio_only=False,
    ),
    "Chromecast HD": ReceiverCaps(
        tier="gtv",
        max_width=1920, max_height=1080, max_fps=60,
        supports_h265=True, supports_av1=True,
        supports_ac3=True, supports_eac3=True,
        audio_only=False,
    ),
    "Chromecast 4K": ReceiverCaps(
        tier="gtv",
        max_width=3840, max_height=2160, max_fps=60,
        supports_h265=True, supports_av1=True,
        supports_ac3=True, supports_eac3=True,
        audio_only=False,
    ),
    "Google TV": ReceiverCaps(
        tier="gtv",
        max_width=3840, max_height=2160, max_fps=60,
        supports_h265=True, supports_av1=True,
        supports_ac3=True, supports_eac3=True,
        audio_only=False,
    ),
}

# Audio-only families — match any model_name in this set.
_AUDIO_ONLY_MODELS = frozenset({
    "Google Nest Hub",
    "Google Home Hub",       # legacy name for the same device
    "Nest Hub Max",
    "Google Home",
    "Google Home Mini",
    "Google Home Max",
    "Nest Audio",
    "Nest Mini",
})


def capabilities_for_model(model_name: str) -> ReceiverCaps:
    """Lookup. Empty / unknown / typo'd model names return the conservative
    `unknown` fallback (1080p30 H.264 AAC, video-capable)."""
    if not model_name or not model_name.strip():
        return _UNKNOWN
    if model_name in _AUDIO_ONLY_MODELS:
        return _AUDIO_ONLY
    return _TABLE.get(model_name, _UNKNOWN)
