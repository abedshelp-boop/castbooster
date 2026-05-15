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
