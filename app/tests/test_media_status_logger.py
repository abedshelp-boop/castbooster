"""Regression tests for `_MediaStatusLogger`'s on_idle gating.

The Chromecast emits an IDLE/INTERRUPTED status update the instant a new
LOAD command arrives — that update describes the *previous* session being
kicked, not our new session ending. If we fire `on_idle` on that, we tear
down the wake lock and WiFi keepalive for the session we just started.

The fix: gate `on_idle` on having observed at least one non-IDLE state.
"""

from __future__ import annotations

from castbooster.caster import _MediaStatusLogger


class _Status:
    """Minimal stand-in for pychromecast's MediaStatus object."""

    def __init__(self, player_state: str, idle_reason=None,
                 content_type="application/vnd.apple.mpegurl",
                 content_id="http://192.168.1.229:38123/s/abc/master.m3u8"):
        self.player_state = player_state
        self.idle_reason = idle_reason
        self.content_type = content_type
        self.content_id = content_id


def test_initial_idle_interrupted_does_not_fire_on_idle():
    """The exact scenario from the bug: play_media → first status is
    IDLE/INTERRUPTED. on_idle MUST NOT fire."""
    fired = []
    logger = _MediaStatusLogger("Dining room TV", on_idle=lambda: fired.append(True))
    logger.new_media_status(_Status("IDLE", idle_reason="INTERRUPTED"))
    assert fired == [], "on_idle fired on the session-start IDLE/INTERRUPTED"


def test_idle_terminal_after_active_does_fire():
    """Real end-of-session: BUFFERING → PLAYING → IDLE/FINISHED. on_idle MUST fire."""
    fired = []
    logger = _MediaStatusLogger("TV", on_idle=lambda: fired.append("ok"))
    logger.new_media_status(_Status("BUFFERING"))
    logger.new_media_status(_Status("PLAYING"))
    logger.new_media_status(_Status("IDLE", idle_reason="FINISHED"))
    assert fired == ["ok"]


def test_idle_terminal_after_paused_fires():
    """PAUSED also counts as an active state — pause → resume → end is real."""
    fired = []
    logger = _MediaStatusLogger("TV", on_idle=lambda: fired.append("ok"))
    logger.new_media_status(_Status("PAUSED"))
    logger.new_media_status(_Status("IDLE", idle_reason="CANCELLED"))
    assert fired == ["ok"]


def test_on_idle_only_fires_once():
    fired = []
    logger = _MediaStatusLogger("TV", on_idle=lambda: fired.append(1))
    logger.new_media_status(_Status("PLAYING"))
    logger.new_media_status(_Status("IDLE", idle_reason="FINISHED"))
    logger.new_media_status(_Status("IDLE", idle_reason="ERROR"))
    assert fired == [1]


def test_idle_with_non_terminal_reason_never_fires():
    """IDLE/None is the 'just loading' state — never terminal."""
    fired = []
    logger = _MediaStatusLogger("TV", on_idle=lambda: fired.append(1))
    logger.new_media_status(_Status("IDLE", idle_reason=None))
    logger.new_media_status(_Status("PLAYING"))
    logger.new_media_status(_Status("IDLE", idle_reason=None))
    assert fired == []


def test_idle_interrupted_after_playing_fires():
    """INTERRUPTED after we've actually played IS a real terminal —
    another sender genuinely took over our session."""
    fired = []
    logger = _MediaStatusLogger("TV", on_idle=lambda: fired.append(1))
    logger.new_media_status(_Status("PLAYING"))
    logger.new_media_status(_Status("IDLE", idle_reason="INTERRUPTED"))
    assert fired == [1]
