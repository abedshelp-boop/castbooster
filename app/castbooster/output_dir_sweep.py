"""Pre-startup sweeper for leftover output dirs from prior crashed/killed runs.

`proxy._handle_cast` writes per-session HLS segments under
`<tempfile.gettempdir()>/castbooster/<token>/v<N>/`. `Transcoder.stop()` —
the only cleanup path — runs `_ProcessSlot._cleanup_output_dir` on session
end. A taskkill /F or hard process crash mid-cast leaks the dir forever.

This module sweeps `<base>/castbooster/*` at proxy startup, after the
duplicate-instance check in main.py has confirmed we own this machine's
castbooster state. Errors are logged + counted but never raised — a stuck
file should not refuse to start the service.

Pattern mirrors `_ProcessSlot._cleanup_output_dir` (transcoder.py:294-306):
try shutil.rmtree once, on OSError sleep 0.5 and retry with
ignore_errors=True. If the retry still fails, count it as an error and
move on.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def sweep_stranded_output_dirs(base: Optional[Path] = None) -> dict[str, int]:
    """Remove every direct child of `(base or tempfile.gettempdir())/castbooster/`.

    Args:
        base: parent of the `castbooster/` dir to sweep. Defaults to
            `tempfile.gettempdir()`. Tests pass a tmp_path to avoid
            touching the real %TEMP%.

    Returns:
        `{"swept": <removed count>, "errors": <failed count>}`.
        Never raises.
    """
    parent_root = Path(base) if base is not None else Path(tempfile.gettempdir())
    castbooster_dir = parent_root / "castbooster"
    if not castbooster_dir.exists():
        return {"swept": 0, "errors": 0}
    try:
        children = list(castbooster_dir.iterdir())
    except OSError as e:
        log.warning("could not list %s: %s", castbooster_dir, e)
        return {"swept": 0, "errors": 0}

    swept = 0
    errors = 0
    for child in children:
        if not child.is_dir():
            continue
        try:
            shutil.rmtree(child)
            swept += 1
            continue
        except FileNotFoundError:
            # Vanished between iterdir() and rmtree() — fine.
            swept += 1
            continue
        except OSError as e:
            log.warning("sweep first-attempt failed for %s: %s — retrying", child, e)
        time.sleep(0.5)
        try:
            shutil.rmtree(child, ignore_errors=True)
            if child.exists():
                log.warning("sweep retry still failed for %s — leaving it", child)
                errors += 1
            else:
                swept += 1
        except Exception:
            log.exception("sweep retry raised unexpectedly for %s", child)
            errors += 1
    return {"swept": swept, "errors": errors}
