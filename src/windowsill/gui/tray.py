"""Windows taskbar (and Linux) tray front-end, built on pystray + Pillow.

Windows has no menu bar text, so the countdown is drawn *into* the tray icon:
a ring showing how much of the window has elapsed, with the remaining hours or
minutes in the middle. The tooltip carries the full status, and left-clicking
opens the detail window.

Like the macOS front-end, the detail window is a separate process so Tkinter
and pystray never contend for a main thread.
"""

from __future__ import annotations

import logging
import subprocess
import sys

import pystray
from PIL import Image, ImageDraw, ImageFont

from ..config import Config
from ..live import read, tray_tooltip, urgency
from .theme import DARK, palette, urgency_colour

log = logging.getLogger("sill.gui.tray")

ICON_SIZE = 64

def _rgba(hex_colour: str) -> tuple[int, int, int, int]:
    hex_colour = hex_colour.lstrip("#")
    return (*(int(hex_colour[i:i + 2], 16) for i in (0, 2, 4)), 255)


# Same palette as the panel, so the tray icon and the window read as one app.
_PALETTE = palette("dark")
COLOURS = {
    level: _rgba(urgency_colour(level, _PALETTE))
    for level in ("normal", "warning", "critical", "expired", "unknown")
}
TRACK = _rgba(DARK.border)


def _load_font(size: int) -> ImageFont.ImageFont:
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_icon(seconds: float | None, progress: float, level: str) -> Image.Image:
    """Draw the tray icon: a progress ring with the remaining time inside."""
    image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    colour = COLOURS.get(level, COLOURS["unknown"])

    inset, width = 3, 7
    box = (inset, inset, ICON_SIZE - inset, ICON_SIZE - inset)
    draw.ellipse(box, outline=TRACK, width=width)

    sweep = max(0.0, min(1.0, progress)) * 360.0
    if sweep > 0:
        # -90 so the ring starts at twelve o'clock, like a clock face.
        draw.arc(box, start=-90, end=-90 + sweep, fill=colour, width=width)

    # ASCII only: the fallback bitmap font used when no TrueType face is
    # available has no em-dash, and renders one as a tofu box.
    if seconds is None:
        label = "?"
    elif seconds <= 0:
        label = "0"
    elif seconds >= 3600:
        label = f"{int(seconds // 3600)}h"
    else:
        label = f"{max(1, int(seconds // 60))}m"

    font = _load_font(26 if len(label) <= 2 else 22)
    left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
    draw.text(
        ((ICON_SIZE - (right - left)) / 2 - left,
         (ICON_SIZE - (bottom - top)) / 2 - top),
        label,
        font=font,
        fill=colour,
    )
    return image


class TrayApp:
    def __init__(self, config: Config, monitor=None) -> None:
        self.config = config
        self.monitor = monitor
        self._panel_process: subprocess.Popen | None = None
        self._last_key: tuple | None = None

        self.icon = pystray.Icon(
            "windowsill",
            icon=render_icon(None, 0.0, "unknown"),
            title="Claude Usage Window",
            menu=pystray.Menu(
                pystray.MenuItem("Open Detail Window", self.open_panel, default=True),
                pystray.MenuItem("Send Re-arm Prompt Now", self.send_trigger),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open Log Folder", self.open_logs),
                pystray.MenuItem("Quit", self.quit_app),
            ),
        )

    # -- refresh ------------------------------------------------------------

    def refresh(self) -> None:
        """Called once a second from the monitor's UI loop."""
        try:
            state = read(self.config.live_file)
        except Exception:
            log.exception("tray refresh failed")
            return

        if not state.connected:
            key = ("disconnected", state.reason)
            if key != self._last_key:
                self.icon.icon = render_icon(None, 0.0, "unknown")
                self.icon.title = tray_tooltip(state)
                self._last_key = key
            return

        seconds = state.remaining_seconds
        level = urgency(seconds)

        # Redraw only when the visible content changes: the icon is rasterised
        # each time, and at one frame per second that adds up for no benefit.
        minutes = int(seconds // 60) if seconds is not None else None
        key = (minutes, level, round(state.progress, 2), state.state)
        if key == self._last_key:
            return
        self._last_key = key

        self.icon.icon = render_icon(seconds, state.progress, level)
        self.icon.title = tray_tooltip(state)

    # -- actions ------------------------------------------------------------

    def open_panel(self, _icon=None, _item=None) -> None:
        if self._panel_process is not None and self._panel_process.poll() is None:
            return
        from .app import spawn_panel

        self._panel_process = spawn_panel()

    def send_trigger(self, _icon=None, _item=None) -> None:
        if self.monitor is None:
            return
        self.monitor.scheduler.add_job(self.monitor._rearm_job, "date")

    def open_logs(self, _icon=None, _item=None) -> None:
        path = str(self.config.state_dir)
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def quit_app(self, _icon=None, _item=None) -> None:
        if self._panel_process is not None and self._panel_process.poll() is None:
            self._panel_process.terminate()
        if self.monitor is not None:
            self.monitor.shutdown()
        self.icon.stop()

    def run(self) -> int:
        self.icon.run()
        return 0
