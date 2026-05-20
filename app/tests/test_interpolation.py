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


def test_init_resolves_bare_model_name_to_bundled_models_dir(tmp_path, monkeypatch):
    """Bare model name like 'rife-v4.6' resolves to <models>/rife-v4.6/.

    rife-ncnn-vulkan resolves bare -m names relative to the rife binary, not
    the cwd. Our binary lives at castbooster/bin/, models at castbooster/models/,
    so we must pre-resolve.
    """
    from castbooster.filters.interpolation import RIFEFilter
    fake_models = tmp_path / "fake_models"
    fake_models.mkdir()
    monkeypatch.setattr("castbooster.license._BUNDLED_MODELS_DIR", fake_models)
    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=256, height=256,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg",
                      model="rife-v4.6")
    assert rife._model == str((fake_models / "rife-v4.6").resolve())


def test_init_keeps_path_like_model_as_is(tmp_path, monkeypatch):
    """If user passes a path-like model (with a separator), use as-is."""
    from castbooster.filters.interpolation import RIFEFilter
    explicit = tmp_path / "custom" / "weights"
    explicit.mkdir(parents=True)
    rife = RIFEFilter(source_fps=24.0, target_fps=60, width=256, height=256,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg",
                      model=str(explicit))
    assert rife._model == str(explicit)


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
    # 2026-05-20: defaulted to rife-v4.6 (not rife-anime) because rife-anime
    # rejects custom -n with "only rife-v4 model support custom numframe and
    # timestep". Verified empirically against the bundled binary.
    # The constructor resolves bare model names to absolute paths under
    # castbooster/models/ because rife resolves bare names relative to its
    # binary directory (castbooster/bin/), not where we ship them.
    assert "-m" in args
    model_arg = args[args.index("-m") + 1]
    assert model_arg.endswith("rife-v4.6") and ("models" in model_arg.replace("\\", "/"))
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


# ---------- _side_task ------------------------------------------------------

class _FakeDecodeProc:
    """Stand-in for the decode ffmpeg Popen. stdout yields a fixed byte stream."""
    def __init__(self, total_bytes: bytes, fail_with: "int | None" = None):
        self.stdout = io.BytesIO(total_bytes)
        self.stderr = io.BytesIO(b"")
        self._fail_with = fail_with
        self._terminated = False

    def poll(self):
        # Pretend we're still running until stdout drains; then exit.
        if self.stdout.tell() >= len(self.stdout.getvalue()):
            return self._fail_with if self._fail_with is not None else 0
        return None

    def terminate(self):
        self._terminated = True

    def wait(self, timeout=None):
        return self._fail_with if self._fail_with is not None else 0

    @property
    def returncode(self):
        return self._fail_with if self._fail_with is not None else 0


def _build_ctx(tmp_path, encoder_stdin=None):
    """Convenience: build a SideTaskContext with sensible defaults."""
    from castbooster.pipeline_spec import SideTaskContext
    if encoder_stdin is None:
        encoder_stdin = io.BytesIO()
    workdir = tmp_path / "wd"
    workdir.mkdir(parents=True, exist_ok=True)
    return SideTaskContext(
        input_url="http://upstream/x.m3u8",
        target_fps=60,
        encoder_stdin=encoder_stdin,
        workdir=workdir,
        cancel_event=threading.Event(),
    )


def _make_rife_with_stubs(tmp_path, monkeypatch, *,
                          source_fps=24.0, target_fps=60, w=64, h=64,
                          decode_bytes=None, decode_fail=None,
                          rife_fail=False, raw_to_pngs_fail=False,
                          pngs_to_raw_fail=False):
    """Construct a RIFEFilter with all subprocess helpers stubbed."""
    from castbooster.filters.interpolation import RIFEFilter

    rife = RIFEFilter(source_fps=source_fps, target_fps=target_fps,
                      width=w, height=h,
                      rife_path="/fake/rife", ffmpeg_path="/fake/ffmpeg")

    # _spawn_decode returns a fixed fake proc
    if decode_bytes is None:
        # default: 2 full input batches' worth of yuv420p frames
        frame_size = w * h * 3 // 2
        frames_per_batch = round(source_fps * 2)  # BATCH_SECONDS
        decode_bytes = b"\xAB" * (frame_size * frames_per_batch * 2)
    fake_decode = _FakeDecodeProc(decode_bytes, fail_with=decode_fail)
    monkeypatch.setattr(rife, "_spawn_decode", lambda url: fake_decode)

    # _rawvideo_chunk_to_pngs writes target_count empty PNG files (placeholders)
    def _fake_raw_to_pngs(chunk, frame_count, in_dir):
        if raw_to_pngs_fail:
            raise RuntimeError("forced raw->PNG failure")
        in_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, frame_count + 1):
            (in_dir / f"{i:08d}.png").write_bytes(b"FAKEPNG")
    monkeypatch.setattr(rife, "_rawvideo_chunk_to_pngs", _fake_raw_to_pngs)

    # _run_rife "produces" target_count PNGs in out_dir
    def _fake_run_rife(in_dir, out_dir, target_count):
        if rife_fail:
            raise RuntimeError("forced rife failure")
        out_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, target_count + 1):
            (out_dir / f"{i:08d}.png").write_bytes(b"FAKEPNG_OUT")
    monkeypatch.setattr(rife, "_run_rife", _fake_run_rife)

    # _pngs_to_rawvideo_chunk returns N target frames worth of zeros
    frame_size = w * h * 3 // 2
    def _fake_pngs_to_raw(out_dir, frame_count):
        if pngs_to_raw_fail:
            raise RuntimeError("forced PNG->raw failure")
        return b"\x00" * (frame_size * frame_count)
    monkeypatch.setattr(rife, "_pngs_to_rawvideo_chunk", _fake_pngs_to_raw)

    return rife, fake_decode


def test_side_task_one_batch_writes_target_fps_rawvideo(tmp_path, monkeypatch):
    """One full input batch -> one full output batch worth of rawvideo to encoder_stdin."""
    # Provide just 1 batch of decode bytes
    frame_size = 64 * 64 * 3 // 2
    decode_bytes = b"\xAB" * (frame_size * 48)  # 48 frames @ source_fps=24 * 2s
    rife, _ = _make_rife_with_stubs(tmp_path, monkeypatch, decode_bytes=decode_bytes)

    ctx = _build_ctx(tmp_path)
    rife._side_task(ctx)

    # 60fps * 2s = 120 output frames * 6144 bytes = 737280 bytes
    expected_bytes = frame_size * 120
    assert ctx.encoder_stdin.tell() == expected_bytes


def test_side_task_two_batches(tmp_path, monkeypatch):
    """Two full input batches -> two output batches written to encoder_stdin."""
    rife, _ = _make_rife_with_stubs(tmp_path, monkeypatch)
    ctx = _build_ctx(tmp_path)
    rife._side_task(ctx)
    frame_size = 64 * 64 * 3 // 2
    assert ctx.encoder_stdin.tell() == frame_size * 120 * 2


def test_side_task_stops_at_eof(tmp_path, monkeypatch):
    """Short read from decode (fewer than full batch) -> loop exits cleanly."""
    frame_size = 64 * 64 * 3 // 2
    partial = b"\xAB" * (frame_size * 5)  # 5 frames, less than 48 = 1 batch
    rife, _ = _make_rife_with_stubs(tmp_path, monkeypatch, decode_bytes=partial)
    ctx = _build_ctx(tmp_path)
    rife._side_task(ctx)
    # No batches completed -> no output bytes
    assert ctx.encoder_stdin.tell() == 0


def test_side_task_respects_cancel_event(tmp_path, monkeypatch):
    """Setting cancel_event causes _side_task to return at the next batch boundary."""
    rife, _ = _make_rife_with_stubs(tmp_path, monkeypatch)
    ctx = _build_ctx(tmp_path)
    ctx.cancel_event.set()  # set BEFORE first batch
    rife._side_task(ctx)
    # cancel before any batch -> no output
    assert ctx.encoder_stdin.tell() == 0


def test_side_task_raises_on_decode_nonzero(tmp_path, monkeypatch):
    """Decode ffmpeg exits non-zero (not from cancel) -> side task raises."""
    frame_size = 64 * 64 * 3 // 2
    decode_bytes = b"\xAB" * (frame_size * 48)  # 1 batch then EOF
    rife, fake_decode = _make_rife_with_stubs(
        tmp_path, monkeypatch, decode_bytes=decode_bytes, decode_fail=1,
    )
    ctx = _build_ctx(tmp_path)
    with pytest.raises(RuntimeError, match="decode ffmpeg"):
        rife._side_task(ctx)


def test_side_task_raises_on_rife_failure(tmp_path, monkeypatch):
    """_run_rife raises -> side task propagates."""
    rife, _ = _make_rife_with_stubs(tmp_path, monkeypatch, rife_fail=True)
    ctx = _build_ctx(tmp_path)
    with pytest.raises(RuntimeError, match="forced rife failure"):
        rife._side_task(ctx)


def test_side_task_raises_on_raw_to_pngs_failure(tmp_path, monkeypatch):
    """_rawvideo_chunk_to_pngs raises -> side task propagates."""
    rife, _ = _make_rife_with_stubs(tmp_path, monkeypatch, raw_to_pngs_fail=True)
    ctx = _build_ctx(tmp_path)
    with pytest.raises(RuntimeError, match="forced raw"):
        rife._side_task(ctx)


def test_side_task_cleanup_lag_calls(tmp_path, monkeypatch):
    """Verify rmtree lag=1: cleanup(N-2) fires after each batch N; final
    finally block cleans up the last two batches that weren't reached by
    the in-loop cleanup.

    For 4 batches, the cleanup call sequence must be:
        in-loop:   cleanup(-2), cleanup(-1), cleanup(0), cleanup(1)
        finally:   cleanup(2), cleanup(3)

    Tracks via monkeypatching _cleanup_batch_dir — robust against
    incidental dir creation/removal by other stubs.
    """
    rife, _ = _make_rife_with_stubs(tmp_path, monkeypatch,
                                    decode_bytes=b"\xAB" * (64 * 64 * 3 // 2 * 48 * 4))

    cleanup_calls: list[int] = []
    orig_cleanup = rife._cleanup_batch_dir
    def _track_cleanup(workdir, batch_idx):
        cleanup_calls.append(batch_idx)
        orig_cleanup(workdir, batch_idx)
    monkeypatch.setattr(rife, "_cleanup_batch_dir", _track_cleanup)

    ctx = _build_ctx(tmp_path)
    rife._side_task(ctx)

    # 4 in-loop calls (one per batch, with N-2 indices) + 2 finally calls
    assert cleanup_calls == [-2, -1, 0, 1, 2, 3], (
        f"unexpected cleanup sequence: {cleanup_calls}"
    )


# ---------- Vulkan-gated integration test ----------------------------------

import os
import subprocess as _real_sub


@pytest.mark.skipif(
    not os.environ.get("RUN_RIFE"),
    reason="set RUN_RIFE=1 to run the real-rife integration test",
)
def test_side_task_end_to_end_24_to_60_with_real_rife(tmp_path):
    """4s test clip @ 24fps -> side task -> BytesIO encoder gets 60fps rawvideo.

    Requires:
      - rife-ncnn-vulkan + rife-anime weights bundled
        (run app/scripts/fetch_rife.ps1 first)
      - Working Vulkan + a GPU
      - ffmpeg vendored

    Verifies:
      - The real rife binary runs to completion.
      - Output byte count is close to the 60fps * 4s expectation.
      - No exception from _side_task.
    """
    from castbooster.filters.interpolation import RIFEFilter
    from castbooster.ffmpeg_probe import locate_ffmpeg
    from castbooster.license import _locate_rife, vulkan_available

    if not vulkan_available():
        pytest.skip("vulkan_available() returned False on this machine")

    ffmpeg = locate_ffmpeg()
    rife_bin = _locate_rife()

    # Generate a 4s 64x64 24fps yuv420p MP4 test clip.
    test_clip = tmp_path / "test_input.mp4"
    gen = _real_sub.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error",
         "-f", "lavfi",
         "-i", "testsrc2=size=256x256:rate=24:duration=4",
         "-pix_fmt", "yuv420p",
         "-c:v", "libx264",
         "-y", str(test_clip)],
        capture_output=True, timeout=60,
    )
    assert gen.returncode == 0, gen.stderr.decode("utf-8", "replace")
    assert test_clip.exists()

    encoder_stdin = io.BytesIO()
    workdir = tmp_path / "wd"
    workdir.mkdir()

    # 2026-05-20: 64x64 caused STATUS_STACK_BUFFER_OVERRUN (0xC0000409) on
    # Qualcomm Adreno X1-45 with rife-v4.6's compute shader. 256x256 is the
    # smallest size verified to survive the shader on this dev box.
    rife = RIFEFilter(
        source_fps=24.0, target_fps=60, width=256, height=256,
        rife_path=rife_bin, ffmpeg_path=ffmpeg,
    )
    from castbooster.pipeline_spec import SideTaskContext
    ctx = SideTaskContext(
        input_url=str(test_clip),
        target_fps=60,
        encoder_stdin=encoder_stdin,
        workdir=workdir,
        cancel_event=threading.Event(),
    )

    rife._side_task(ctx)

    # Expected: 4s * 24fps = 96 input frames = 2 input batches of 48 frames.
    # Each input batch -> 120 output frames (60fps * 2s).
    # Total: 240 output frames * 98304 bytes (256*256*1.5) = 23592960 bytes.
    frame_size = 256 * 256 * 3 // 2
    expected = frame_size * 120 * 2
    actual = encoder_stdin.tell()
    # Allow up to one batch of slack in case rife rounds to N-1 frames in a batch.
    tolerance = frame_size * 120
    assert abs(actual - expected) <= tolerance, (
        f"expected ~{expected} bytes (240 frames), got {actual} bytes "
        f"({actual // frame_size} frames)"
    )
