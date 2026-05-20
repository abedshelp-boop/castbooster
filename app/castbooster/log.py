import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / "CastBooster"
    d.mkdir(parents=True, exist_ok=True)
    return d


LOG_PATH = _log_dir() / "castbooster.log"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Initialize the root logger.

    Honors env var CASTBOOSTER_LOG_LEVEL (DEBUG / INFO / WARNING / ERROR /
    CRITICAL — case-insensitive) which overrides the default. Useful for
    diagnostic runs where ffmpeg stderr (logged at DEBUG by Transcoder)
    needs to be captured in the log file.
    """
    root = logging.getLogger()
    if getattr(setup_logging, "_done", False):
        return root

    env_level = os.environ.get("CASTBOOSTER_LOG_LEVEL", "").strip().upper()
    if env_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level = getattr(logging, env_level)

    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # stderr only if attached to a console — PyInstaller windowed builds have no stdout.
    if sys.stderr and sys.stderr.isatty():
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    setup_logging._done = True
    return root
