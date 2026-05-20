import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pystray
from PIL import Image, ImageDraw

from castbooster import __version__
from castbooster.log import LOG_PATH
from castbooster.netinfo import get_lan_ip

log = logging.getLogger(__name__)

_TRAY_ICON_PATH = Path(__file__).resolve().parent / "assets" / "tray-icon.png"


def _icon_image() -> Image.Image:
    """Load the Aperture Lock tray icon, falling back to a placeholder if the
    asset is missing (e.g. running from a partial checkout)."""
    try:
        return Image.open(_TRAY_ICON_PATH).convert("RGBA")
    except Exception:
        log.warning("tray: icon asset missing at %s — using placeholder", _TRAY_ICON_PATH)
        img = Image.new("RGB", (64, 64), color=(30, 90, 180))
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill=(255, 255, 255))
        d.text((18, 22), "CB", fill=(30, 90, 180))
        return img


def _open_log(icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(LOG_PATH))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(LOG_PATH)])
    except Exception:
        log.exception("open_log failed")


def _tail_log(n: int = 200) -> str:
    """Return the last `n` lines of the rotating log file as a single string."""
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # 64 KB tail is plenty for 200 typical INFO lines.
            f.seek(max(0, size - 64 * 1024))
            blob = f.read().decode("utf-8", errors="replace")
        lines = blob.splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"<failed to read {LOG_PATH}: {e}>"


def _copy_diagnostic_bundle(icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
    """Compose a paste-ready bundle (version + LAN IP + last 200 log lines)
    and shove it onto the Windows clipboard via clip.exe.

    Designed for the "click → paste into bug report" loop during dev/test."""
    try:
        bundle_lines = [
            "=== Cast Booster Diagnostic Bundle ===",
            f"version : {__version__}",
            f"lan_ip  : {get_lan_ip()}",
            f"log_path: {LOG_PATH}",
            "=== last 200 log lines ===",
            _tail_log(200),
        ]
        bundle = "\n".join(bundle_lines)
        if sys.platform == "win32":
            # clip.exe accepts UTF-16 LE on stdin; covers non-ASCII safely.
            subprocess.run(
                ["clip"],
                input=bundle.encode("utf-16-le"),
                check=True,
                timeout=5,
            )
        else:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=bundle.encode("utf-8"),
                check=True,
                timeout=5,
            )
        log.info("diagnostic bundle copied to clipboard (%d chars)", len(bundle))
    except Exception:
        log.exception("copy_diagnostic_bundle failed")


def run_tray(on_quit: Callable[[], None]) -> None:
    """Blocks the calling (main) thread until the user picks Quit."""

    def _quit(icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        log.info("tray: quit requested")
        icon.visible = False
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem("Cast — (no stream registered)", lambda *a: None, enabled=False),
        pystray.MenuItem("Open log", _open_log),
        pystray.MenuItem("Copy diagnostic bundle", _copy_diagnostic_bundle),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"Cast Booster v{__version__}", lambda *a: None, enabled=False),
        pystray.MenuItem("Quit", _quit),
    )

    icon = pystray.Icon(
        name="castbooster",
        icon=_icon_image(),
        title=f"Cast Booster v{__version__}",
        menu=menu,
    )
    log.info("tray: starting")
    icon.run()
    log.info("tray: exited")
