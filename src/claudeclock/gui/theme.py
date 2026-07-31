"""Claude's visual language, in one place.

Claude's interface is built on a warm paper-coloured ground rather than the
usual cold grey, with a single terracotta accent doing all the emphatic work.
Reproducing that faithfully is mostly restraint: one accent, generous
whitespace, quiet borders, and type that is small and calm except for the one
number that matters.

Colours are taken from Claude's own surfaces. Both schemes are defined because
the panel follows the OS appearance.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    bg: str            # page ground
    surface: str       # raised card
    border: str        # hairline dividers
    text: str          # primary copy
    muted: str         # secondary copy
    subtle: str        # tertiary / captions
    accent: str        # Claude terracotta - the single emphatic colour
    accent_soft: str   # accent at low contrast, for tracks and fills
    warning: str
    critical: str
    expired: str
    ok: str


# Claude's signature "bone" ground with terracotta accent.
LIGHT = Palette(
    bg="#F0EEE6",
    surface="#FAF9F5",
    border="#DDDACE",
    text="#141413",
    muted="#6C6B66",
    subtle="#91908A",
    accent="#D97757",
    accent_soft="#E8D5CB",
    warning="#B8862F",
    critical="#BC4C3A",
    expired="#7C6BAD",
    ok="#5A9367",
)

DARK = Palette(
    bg="#1F1E1D",
    surface="#262624",
    border="#3A3937",
    text="#F5F4EF",
    muted="#A5A39C",
    subtle="#7C7A73",
    accent="#E08356",
    accent_soft="#4A3730",
    warning="#D9A441",
    critical="#E06C55",
    expired="#9B8ACB",
    ok="#6FAE7C",
)


def system_prefers_dark() -> bool:
    """Best-effort read of the OS appearance. Defaults to light."""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            # The key is absent entirely in light mode, which is why this reads
            # the value rather than a boolean.
            return "dark" in result.stdout.strip().lower()
        except (OSError, subprocess.SubprocessError):
            return False

    if sys.platform.startswith("win"):
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
        except (ImportError, OSError):
            return False

    return False


def palette(theme: str = "dark") -> Palette:
    """`dark` (default), `light`, or `auto` to follow the OS appearance."""
    if theme == "auto":
        return DARK if system_prefers_dark() else LIGHT
    return LIGHT if theme == "light" else DARK


def urgency_colour(level: str, p: Palette) -> str:
    """Map an urgency band to a colour.

    `normal` deliberately uses the terracotta accent rather than a green: in
    Claude's palette the accent *is* the healthy, in-progress state, and
    reserving red-adjacent hues for genuine urgency keeps them meaningful.
    """
    return {
        "normal": p.accent,
        "warning": p.warning,
        "critical": p.critical,
        "expired": p.expired,
        "unknown": p.subtle,
    }.get(level, p.subtle)


# Type stack: Claude uses Styrene A / Tiempos, neither of which we can ship.
# These are the closest system faces on each platform.
def font_stack() -> tuple[str, str]:
    """Return (ui_family, numeric_family)."""
    if sys.platform == "darwin":
        return ("SF Pro Text", "SF Pro Display")
    if sys.platform.startswith("win"):
        return ("Segoe UI", "Segoe UI")
    return ("DejaVu Sans", "DejaVu Sans")
