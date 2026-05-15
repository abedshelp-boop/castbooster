"""Unit tests for Transcoder.set_filter_chain() and the RELOADING state.

Strategy: same as test_transcoder_lifecycle.py — patch subprocess.Popen
to return queued FakeFfmpegProcess instances we drive directly. Each test
queues TWO fakes (one per slot) because a reload spawns two ffmpegs.
"""
from __future__ import annotations

import pytest


# ---------- R14: subtitle_stream_missing fatal pattern -----------------------

def test_subtitle_stream_missing_pattern_classified():
    """The new fatal pattern for stream-spec-mismatch errors."""
    from castbooster.transcoder import _classify_stderr_line
    line = (
        "Stream specifier 's:0' in filtergraph description "
        "'subtitles=foo.mkv:si=0' matches no streams."
    )
    assert _classify_stderr_line(line) == "subtitle_stream_missing"
