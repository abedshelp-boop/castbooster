"""Unit tests for castbooster.pipeline_spec.

The module defines two frozen dataclasses consumed by transcoder code
(P3.3) and produced by RIFEFilter (P3.2). P3.1 ships the shape only.
"""
from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest


def test_pipeline_spec_imports():
    """Module exists and exposes both dataclasses."""
    from castbooster.pipeline_spec import PipelineSpec, SideTaskContext  # noqa: F401


def test_side_task_context_holds_all_fields():
    """SideTaskContext is a frozen dataclass with the documented fields."""
    from castbooster.pipeline_spec import SideTaskContext
    cancel = threading.Event()
    stdin = io.BytesIO()
    ctx = SideTaskContext(
        input_url="http://example.com/x.m3u8",
        target_fps=60,
        encoder_stdin=stdin,
        workdir=Path("/tmp/x"),
        cancel_event=cancel,
    )
    assert ctx.input_url == "http://example.com/x.m3u8"
    assert ctx.target_fps == 60
    assert ctx.encoder_stdin is stdin
    assert ctx.workdir == Path("/tmp/x")
    assert ctx.cancel_event is cancel


def test_side_task_context_is_frozen():
    """SideTaskContext refuses mutation (frozen=True)."""
    from castbooster.pipeline_spec import SideTaskContext
    ctx = SideTaskContext(
        input_url="x",
        target_fps=30,
        encoder_stdin=io.BytesIO(),
        workdir=Path("/tmp"),
        cancel_event=threading.Event(),
    )
    with pytest.raises(AttributeError):
        ctx.target_fps = 60  # type: ignore[misc]


def test_pipeline_spec_holds_all_fields():
    """PipelineSpec is a frozen dataclass with the documented fields."""
    from castbooster.pipeline_spec import PipelineSpec, SideTaskContext

    def _stub_side_task(ctx: SideTaskContext) -> None:
        return None

    spec = PipelineSpec(
        encoder_input_format="rawvideo:yuv420p:1920x1080",
        target_fps=60,
        side_task_factory=_stub_side_task,
    )
    assert spec.encoder_input_format == "rawvideo:yuv420p:1920x1080"
    assert spec.target_fps == 60
    assert spec.side_task_factory is _stub_side_task


def test_pipeline_spec_is_frozen():
    """PipelineSpec refuses mutation (frozen=True)."""
    from castbooster.pipeline_spec import PipelineSpec
    spec = PipelineSpec(
        encoder_input_format="rawvideo:yuv420p:64x64",
        target_fps=30,
        side_task_factory=lambda ctx: None,
    )
    with pytest.raises(AttributeError):
        spec.target_fps = 60  # type: ignore[misc]
