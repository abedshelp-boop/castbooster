"""Unit tests for castbooster.filter_chain.

Strategy: pure-function tests on the render() outputs. No I/O, no threading.
"""
from __future__ import annotations

import pytest


def test_noop_renders_null():
    from castbooster.filter_chain import NoopFilter
    assert NoopFilter().render() == "null"


def test_filter_chain_empty_defaults_to_noop():
    from castbooster.filter_chain import FilterChain
    assert FilterChain().render("any-url") == "null"


def test_filter_chain_single_noop():
    from castbooster.filter_chain import FilterChain, NoopFilter
    chain = FilterChain([NoopFilter()])
    assert chain.render("any-url") == "null"


def test_filter_chain_keeps_null_when_only_stage():
    """All-noop chain renders 'null', not empty string."""
    from castbooster.filter_chain import FilterChain, NoopFilter
    chain = FilterChain([NoopFilter(), NoopFilter()])
    assert chain.render("any-url") == "null"


def test_custom_filterstage_protocol_works():
    """A user-defined class with .render() composes via FilterChain."""
    from castbooster.filter_chain import FilterChain, NoopFilter

    class FakeScaleFilter:
        def render(self) -> str:
            return "scale=1280:720"

    chain = FilterChain([NoopFilter(), FakeScaleFilter()])
    assert chain.render("any-url") == "scale=1280:720"


# ---------- SubtitleBurnIn ---------------------------------------------------

def test_subtitle_burnin_requires_bind():
    """Calling render() before FilterChain bound an input raises RuntimeError."""
    from castbooster.filter_chain import SubtitleBurnIn
    stage = SubtitleBurnIn()
    with pytest.raises(RuntimeError):
        stage.render()


def test_subtitle_burnin_renders_subtitles_filter():
    """After _bind('/path/to/src.mkv'), render() returns subtitles='...':si=0."""
    from castbooster.filter_chain import SubtitleBurnIn
    stage = SubtitleBurnIn()
    stage._bind("/path/to/src.mkv")
    assert stage.render() == "subtitles='/path/to/src.mkv':si=0"


def test_subtitle_burnin_stream_index_2():
    """SubtitleBurnIn(stream_index=2) renders ...:si=2."""
    from castbooster.filter_chain import SubtitleBurnIn
    stage = SubtitleBurnIn(stream_index=2)
    stage._bind("/x.mkv")
    assert stage.render() == "subtitles='/x.mkv':si=2"


def test_subtitle_burnin_escapes_windows_path():
    """A Windows path is normalized to forward slashes; the drive-letter
    colon is backslash-escaped to satisfy ffmpeg's AVOption parser."""
    from castbooster.filter_chain import SubtitleBurnIn
    stage = SubtitleBurnIn()
    stage._bind(r"C:\Users\Abeds\AppData\Local\Temp\castbooster\token\v1\input.mkv")
    expected = (
        "subtitles='C\\:/Users/Abeds/AppData/Local/Temp/"
        "castbooster/token/v1/input.mkv':si=0"
    )
    assert stage.render() == expected


def test_subtitle_burnin_escapes_apostrophe():
    """A path containing a literal ' is spliced as '\\'' per ffmpeg docs."""
    from castbooster.filter_chain import SubtitleBurnIn
    stage = SubtitleBurnIn()
    stage._bind("/x/Crime d'Amour.mkv")
    # The ffmpeg-style splice: close quote, escaped quote, reopen quote
    assert stage.render() == "subtitles='/x/Crime d'\\''Amour.mkv':si=0"


def test_filter_chain_subtitle_only():
    """A chain with only SubtitleBurnIn renders just the subtitles filter."""
    from castbooster.filter_chain import FilterChain, SubtitleBurnIn
    chain = FilterChain([SubtitleBurnIn()])
    assert chain.render("/x.mkv") == "subtitles='/x.mkv':si=0"


def test_filter_chain_drops_null_when_compositing():
    """When composing N>1 stages, literal 'null' fragments are dropped."""
    from castbooster.filter_chain import FilterChain, NoopFilter, SubtitleBurnIn
    chain = FilterChain([NoopFilter(), SubtitleBurnIn()])
    assert chain.render("/x.mkv") == "subtitles='/x.mkv':si=0"


def test_filter_chain_binds_input_to_subtitleburnin():
    """After render(url), the SubtitleBurnIn stage has _input_url set."""
    from castbooster.filter_chain import FilterChain, SubtitleBurnIn
    stage = SubtitleBurnIn()
    chain = FilterChain([stage])
    chain.render("/some/path.mkv")
    assert stage._input_url == "/some/path.mkv"
