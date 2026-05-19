"""Unit tests for castbooster.filters.interpolation.

Strategy: pure-function tests on construction / render / pipeline_spec.
Side-task tests live in Tasks 5-6 below.
"""
from __future__ import annotations

import io
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------- construction + shape -------------------------------------------

def test_render_returns_null():
    """RIFEFilter does its filtering via the side task, not via -vf."""
    from castbooster.filters.interpolation import RIFEFilter
    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg")
    assert rife.render() == "null"


def test_pipeline_spec_shape():
    """pipeline_spec returns a frozen PipelineSpec with the documented fields."""
    from castbooster.filters.interpolation import RIFEFilter
    from castbooster.pipeline_spec import PipelineSpec
    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=1920, height=1080,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg")
    spec = rife.pipeline_spec()
    assert isinstance(spec, PipelineSpec)
    assert spec.encoder_input_format == "rawvideo:yuv420p:1920x1080"
    assert spec.target_fps == 60
    assert callable(spec.side_task_factory)


def test_pipeline_spec_side_task_factory_is_bound_method():
    """side_task_factory must be the RIFEFilter._side_task bound method.

    P3.3's _ProcessSlot calls spec.side_task_factory(ctx) on a fresh Thread.
    """
    from castbooster.filters.interpolation import RIFEFilter
    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg")
    spec = rife.pipeline_spec()
    assert spec.side_task_factory.__func__ is RIFEFilter._side_task


def test_filterchain_with_rife_renders_null():
    """FilterChain.render() must still produce 'null' for a RIFE-only chain.

    The chain uses .render() to build the -vf fragment; pipeline_spec() is
    consulted separately by the (P3.3) transcoder.
    """
    from castbooster.filter_chain import FilterChain
    from castbooster.filters.interpolation import RIFEFilter
    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg")
    chain = FilterChain([rife])
    assert chain.render("any-url") == "null"


def test_init_locates_rife_and_ffmpeg_when_not_given(tmp_path, monkeypatch):
    """When rife_path/ffmpeg_path are None, RIFEFilter discovers via license + ffmpeg_probe."""
    from castbooster.filters.interpolation import RIFEFilter

    fake_rife = tmp_path / "rife-ncnn-vulkan.exe"
    fake_rife.write_bytes(b"x")
    fake_ffmpeg = tmp_path / "ffmpeg.exe"
    fake_ffmpeg.write_bytes(b"y")

    monkeypatch.setattr("castbooster.license._BUNDLED_RIFE", fake_rife)
    monkeypatch.setenv("CASTBOOSTER_FFMPEG", str(fake_ffmpeg))

    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64)
    assert rife._rife_path == str(fake_rife.resolve())
    assert rife._ffmpeg_path == str(fake_ffmpeg.resolve())
