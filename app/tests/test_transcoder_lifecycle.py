"""Unit tests for castbooster.transcoder.

Strategy: patch subprocess.Popen to return a FakeFfmpegProcess we drive
directly from the test. Each test manipulates the fake (queue stderr lines,
set exit code, accept stdin) AND directly touches files in the output dir;
the 250ms poller picks up the changes. Tests pass _poll_interval=0.05 to
keep CI fast.
"""
from __future__ import annotations

import io
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest


# ---------- FakeFfmpegProcess ------------------------------------------------

class _FakeStderr:
    """Blocking-iterator stderr backed by a queue. yields bytes lines until
    .close() pushes the sentinel and StopIteration is raised."""

    _SENTINEL = object()

    def __init__(self) -> None:
        self._q: "queue.Queue[object]" = queue.Queue()

    def queue_line(self, line: bytes) -> None:
        self._q.put(line)

    def close(self) -> None:
        self._q.put(self._SENTINEL)

    def __iter__(self) -> "_FakeStderr":
        return self

    def __next__(self) -> bytes:
        item = self._q.get()  # blocks
        if item is self._SENTINEL:
            raise StopIteration
        assert isinstance(item, bytes)
        return item


class FakeFfmpegProcess:
    """Minimal subprocess.Popen stand-in. Tests drive .set_exit() and
    .queue_stderr() to simulate ffmpeg behavior."""

    def __init__(self) -> None:
        self.stdin = io.BytesIO()
        self.stderr = _FakeStderr()
        self._exit_code: Optional[int] = None
        self._exit_event = threading.Event()

    # --- Popen-compatible surface ---
    def poll(self) -> Optional[int]:
        return self._exit_code

    def wait(self, timeout: Optional[float] = None) -> int:
        if self._exit_event.wait(timeout):
            assert self._exit_code is not None
            return self._exit_code
        raise subprocess.TimeoutExpired(cmd="fake-ffmpeg", timeout=timeout)

    def kill(self) -> None:
        self.set_exit(-9)

    def terminate(self) -> None:  # included for API parity; not used
        self.set_exit(-15)

    # --- Test-driver surface ---
    def set_exit(self, code: int) -> None:
        if self._exit_code is None:
            self._exit_code = code
            self.stderr.close()
            self._exit_event.set()

    def queue_stderr(self, line: str) -> None:
        if not line.endswith("\n"):
            line += "\n"
        self.stderr.queue_line(line.encode("utf-8"))


# ---------- Smoke test for the helper itself ---------------------------------

def test_fake_ffmpeg_process_basic_lifecycle():
    fake = FakeFfmpegProcess()
    assert fake.poll() is None

    # Queue a stderr line and consume it
    fake.queue_stderr("hello")
    assert next(iter(fake.stderr)) == b"hello\n"

    # Set exit code → poll/wait return it
    fake.set_exit(0)
    assert fake.poll() == 0
    assert fake.wait(timeout=0.1) == 0

    # Stderr iterator now stops cleanly
    with pytest.raises(StopIteration):
        next(iter(fake.stderr))


def test_fake_ffmpeg_kill_sets_exit_neg9():
    fake = FakeFfmpegProcess()
    fake.kill()
    assert fake.poll() == -9


def test_fake_ffmpeg_wait_times_out_when_not_exited():
    fake = FakeFfmpegProcess()
    with pytest.raises(subprocess.TimeoutExpired):
        fake.wait(timeout=0.05)
