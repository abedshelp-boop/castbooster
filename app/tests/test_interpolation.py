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


# ---------- _spawn_decode ---------------------------------------------------

import subprocess


def test_spawn_decode_argv(monkeypatch):
    """ffmpeg-decode argv reads URL -> rawvideo yuv420p on stdout, no audio."""
    from castbooster.filters.interpolation import RIFEFilter

    captured = {}

    class _FakePopen:
        def __init__(self, args, **kw):
            captured["args"] = args
            captured["kw"] = kw
            self.stdout = MagicMock()
            self.stderr = MagicMock()

    monkeypatch.setattr("castbooster.filters.interpolation.subprocess.Popen", _FakePopen)
    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=1920, height=1080,
                      rife_path="/fake/rife.exe", ffmpeg_path="/fake/ffmpeg.exe")

    proc = rife._spawn_decode("http://upstream/x.m3u8")
    args = captured["args"]
    # binary first
    assert args[0] == "/fake/ffmpeg.exe"
    # input URL must appear after -i
    assert "-i" in args
    assert args[args.index("-i") + 1] == "http://upstream/x.m3u8"
    # output format must be rawvideo yuv420p to stdout
    assert "-f" in args
    f_idxs = [i for i, a in enumerate(args) if a == "-f"]
    assert any(args[i + 1] == "rawvideo" for i in f_idxs)
    assert "-pix_fmt" in args
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"
    # audio dropped (we only forward video to rife)
    assert "-an" in args
    # output target is stdout
    assert args[-1] == "-"
    # we get a Popen-like back
    assert proc.stdout is not None


# ---------- _rawvideo_chunk_to_pngs ----------------------------------------

def test_rawvideo_chunk_to_pngs_argv(tmp_path, monkeypatch):
    """Inner ffmpeg writer takes rawvideo on stdin, writes %08d.png to in_dir."""
    from castbooster.filters.interpolation import RIFEFilter

    captured = {}

    class _FakeWriterPopen:
        def __init__(self, args, **kw):
            captured["args"] = args
            self.stdin = io.BytesIO()
            self.returncode = 0

        def communicate(self, input=None, timeout=None):
            # Sink the bytes; nothing else to do
            return (b"", b"")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("castbooster.filters.interpolation.subprocess.Popen", _FakeWriterPopen)

    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg.exe")
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    chunk = b"\x00" * (64 * 64 * 3 // 2 * 48)  # 48 frames of 64x64 yuv420p
    rife._rawvideo_chunk_to_pngs(chunk, frame_count=48, in_dir=in_dir)

    args = captured["args"]
    assert args[0] == "/fake/ffmpeg.exe"
    assert "-f" in args and args[args.index("-f") + 1] == "rawvideo"
    assert "-pix_fmt" in args and args[args.index("-pix_fmt") + 1] == "yuv420p"
    assert "-video_size" in args and args[args.index("-video_size") + 1] == "64x64"
    assert "-framerate" in args
    # rife doesn't care about framerate for folder mode; just consistent value
    assert "-frames:v" in args and args[args.index("-frames:v") + 1] == "48"
    # output: PNG pattern in in_dir
    out_pattern = str(in_dir / "%08d.png")
    assert args[-1] == out_pattern


def test_rawvideo_chunk_to_pngs_raises_on_nonzero(tmp_path, monkeypatch):
    """Non-zero exit from inner writer raises RuntimeError with stderr."""
    from castbooster.filters.interpolation import RIFEFilter

    class _FailWriterPopen:
        def __init__(self, args, **kw):
            self.stdin = io.BytesIO()
            self.returncode = 1

        def communicate(self, input=None, timeout=None):
            return (b"", b"ffmpeg error: bad pix_fmt")

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr("castbooster.filters.interpolation.subprocess.Popen", _FailWriterPopen)

    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg")
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    with pytest.raises(RuntimeError, match="ffmpeg error: bad pix_fmt"):
        rife._rawvideo_chunk_to_pngs(b"x", frame_count=1, in_dir=in_dir)


# ---------- _run_rife -------------------------------------------------------

def test_run_rife_argv(tmp_path, monkeypatch):
    """rife argv must be -i in -o out -n target_count -m model. No -g, no -s."""
    from castbooster.filters.interpolation import RIFEFilter

    captured = {}

    def _fake_run(args, **kw):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("castbooster.filters.interpolation.subprocess.run", _fake_run)

    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64,
                      rife_path="/fake/rife.exe", ffmpeg_path="/fake/ffmpeg")
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()

    rife._run_rife(in_dir, out_dir, target_count=120)

    args = captured["args"]
    assert args[0] == "/fake/rife.exe"
    assert "-i" in args and args[args.index("-i") + 1] == str(in_dir)
    assert "-o" in args and args[args.index("-o") + 1] == str(out_dir)
    assert "-n" in args and args[args.index("-n") + 1] == "120"
    assert "-m" in args and args[args.index("-m") + 1] == "rife-anime"
    # No GPU flag (P3.2 — default auto; Pillar 5 introduces -g)
    assert "-g" not in args
    # No time-step flag (two-image mode only)
    assert "-s" not in args


def test_run_rife_raises_on_nonzero(tmp_path, monkeypatch):
    """Non-zero rife exit raises RuntimeError with stderr."""
    from castbooster.filters.interpolation import RIFEFilter

    def _fake_run(args, **kw):
        return subprocess.CompletedProcess(
            args=args, returncode=2,
            stdout=b"", stderr=b"failed to find Vulkan device",
        )

    monkeypatch.setattr("castbooster.filters.interpolation.subprocess.run", _fake_run)

    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg")
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    with pytest.raises(RuntimeError, match="failed to find Vulkan device"):
        rife._run_rife(in_dir, out_dir, target_count=120)


# ---------- _pngs_to_rawvideo_chunk ----------------------------------------

def test_pngs_to_rawvideo_chunk_argv_and_bytes(tmp_path, monkeypatch):
    """Inner ffmpeg reader takes %08d.png from out_dir, returns rawvideo bytes."""
    from castbooster.filters.interpolation import RIFEFilter

    captured = {}
    fake_rawvideo = b"\x42" * (64 * 64 * 3 // 2 * 120)  # 120 yuv420p frames

    class _FakeReaderPopen:
        def __init__(self, args, **kw):
            captured["args"] = args
            self.stdout = io.BytesIO(fake_rawvideo)
            self.returncode = 0

        def communicate(self, input=None, timeout=None):
            return (fake_rawvideo, b"")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("castbooster.filters.interpolation.subprocess.Popen", _FakeReaderPopen)

    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg.exe")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    result = rife._pngs_to_rawvideo_chunk(out_dir, frame_count=120)

    args = captured["args"]
    assert args[0] == "/fake/ffmpeg.exe"
    assert "-i" in args and args[args.index("-i") + 1] == str(out_dir / "%08d.png")
    assert "-f" in args and args[args.index("-f") + 1] == "rawvideo"
    assert "-pix_fmt" in args and args[args.index("-pix_fmt") + 1] == "yuv420p"
    assert "-frames:v" in args and args[args.index("-frames:v") + 1] == "120"
    assert args[-1] == "-"
    assert result == fake_rawvideo


def test_pngs_to_rawvideo_chunk_raises_on_nonzero(tmp_path, monkeypatch):
    """Non-zero exit raises RuntimeError with stderr."""
    from castbooster.filters.interpolation import RIFEFilter

    class _FailReaderPopen:
        def __init__(self, args, **kw):
            self.stdout = io.BytesIO()
            self.returncode = 1

        def communicate(self, input=None, timeout=None):
            return (b"", b"PNG decode error")

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr("castbooster.filters.interpolation.subprocess.Popen", _FailReaderPopen)

    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with pytest.raises(RuntimeError, match="PNG decode error"):
        rife._pngs_to_rawvideo_chunk(out_dir, frame_count=120)


# ---------- _cleanup_batch_dir ---------------------------------------------

def test_cleanup_batch_dir_removes_existing(tmp_path):
    """Existing in/N and out/N are removed."""
    from castbooster.filters.interpolation import RIFEFilter
    workdir = tmp_path
    (workdir / "in" / "5").mkdir(parents=True)
    (workdir / "out" / "5").mkdir(parents=True)
    (workdir / "in" / "5" / "00000001.png").write_bytes(b"x")
    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg")
    rife._cleanup_batch_dir(workdir, batch_idx=5)
    assert not (workdir / "in" / "5").exists()
    assert not (workdir / "out" / "5").exists()


def test_cleanup_batch_dir_swallows_missing(tmp_path):
    """Cleanup of a non-existent batch index is a no-op (not an error)."""
    from castbooster.filters.interpolation import RIFEFilter
    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg")
    # Must not raise even though in/99 and out/99 don't exist
    rife._cleanup_batch_dir(tmp_path, batch_idx=99)


def test_cleanup_batch_dir_negative_index_noop(tmp_path):
    """batch_idx < 0 is a no-op (used at the very first batch)."""
    from castbooster.filters.interpolation import RIFEFilter
    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=64, height=64,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg")
    rife._cleanup_batch_dir(tmp_path, batch_idx=-1)
