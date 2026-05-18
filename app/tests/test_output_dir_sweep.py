"""Tests for sweep_stranded_output_dirs — pre-startup cleanup of
leftover per-session output dirs from prior crashed/killed runs."""
from pathlib import Path

import pytest

from castbooster.output_dir_sweep import sweep_stranded_output_dirs


def test_empty_castbooster_parent_is_noop(tmp_path: Path) -> None:
    parent = tmp_path / "castbooster"
    parent.mkdir()
    result = sweep_stranded_output_dirs(base=tmp_path)
    assert result == {"swept": 0, "errors": 0}
    assert parent.exists()  # parent itself preserved


def test_three_token_dirs_all_removed(tmp_path: Path) -> None:
    parent = tmp_path / "castbooster"
    for tok in ("token-aaaa", "token-bbbb", "token-cccc"):
        slot = parent / tok / "v1"
        slot.mkdir(parents=True)
        (slot / "seg_00000.ts").write_bytes(b"\x47" * 188)
        (slot / "variant.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    result = sweep_stranded_output_dirs(base=tmp_path)
    assert result == {"swept": 3, "errors": 0}
    assert not (parent / "token-aaaa").exists()
    assert not (parent / "token-bbbb").exists()
    assert not (parent / "token-cccc").exists()


def test_missing_parent_is_silent_noop(tmp_path: Path) -> None:
    # No castbooster/ at all under tmp_path
    result = sweep_stranded_output_dirs(base=tmp_path)
    assert result == {"swept": 0, "errors": 0}


def test_locked_dir_increments_errors_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "castbooster"
    for tok in ("good-1", "locked", "good-2"):
        (parent / tok / "v1").mkdir(parents=True)
    locked_path = parent / "locked"

    import shutil
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):
        # Raise on every call against the locked dir, even retry-with-
        # ignore_errors. Lets us prove the warn-and-continue path.
        if Path(path) == locked_path:
            raise OSError("simulated lock")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("castbooster.output_dir_sweep.shutil.rmtree", flaky_rmtree)
    result = sweep_stranded_output_dirs(base=tmp_path)
    assert result == {"swept": 2, "errors": 1}
    assert not (parent / "good-1").exists()
    assert not (parent / "good-2").exists()
    assert locked_path.exists()  # locked one still present
