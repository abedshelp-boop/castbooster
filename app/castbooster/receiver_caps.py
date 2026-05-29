"""Static Chromecast receiver capability table.

pychromecast exposes `cast_info.model_name` as a string. We map it to a
ReceiverCaps record telling the transcoder + filter chain what the device
can decode and display. Pillar 3 (RIFE) uses `max_fps` to pick interpolation
target — e.g. gating 60fps output on `max_fps == 60`.

Optimistic-default rule (Pillar 3.5, 2026-05-20): unknown or ambiguous
models still get tier="unknown" + zeros, but the bare "Chromecast" string
(which pychromecast returns for 1st/2nd/3rd gen indistinguishably) defaults
to 1080p60 because 3rd gen is the modal case in 2026. Older hardware
degrades gracefully by decoding-and-downsampling internally.
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
        # Pillar 3.5: optimistic 60fps default. 3rd-gen Chromecast (2018+)
        # is the modal device that reports this exact model_name string and
        # supports 1080p60. 1st/2nd-gen (2013/2015) cap at 30fps but cope
        # with a 60fps input by decoding-and-downsampling internally.
        # Resolution stays 1920x1080 (the 1st/2nd-gen cap is still real).
        max_width=1920, max_height=1080, max_fps=60,
        supports_h265=False,
        supports_av1=False,
        supports_ac3=False,
        supports_eac3=False,
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
    # 2024 device. Caught 2026-05-29 when Abed's "Dining room TV" reported
    # this model_name and fell through to _UNKNOWN (1080p30), causing smooth
    # to be skipped for 60fps mux sources via the source_meets_target gate
    # in _build_filter_chain.
    "Google TV Streamer": ReceiverCaps(
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
