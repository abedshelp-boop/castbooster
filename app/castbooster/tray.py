import logging
import os
import sys
from pathlib import Path
from typing import Callable

import pystray
from PIL import Image, ImageDraw

from castbooster import __version__
from castbooster.log import LOG_PATH

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
            import subprocess

            subprocess.Popen(["xdg-open", str(LOG_PATH)])
    except Exception:
        log.exception("open_log failed")


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
