"""GUI front-end logic that can be verified without a display.

The toolkits themselves (rumps' NSStatusItem, pystray's icon loop) need a real
session, so what is tested here is everything factored out of them: the tray
icon raster, and the panel's construction and render pass against an offscreen
Tk root.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from windowsill import live
from windowsill.tracker import State, WindowView

pytest.importorskip("PIL", reason="tray icon rendering needs Pillow")

from windowsill.gui.tray import ICON_SIZE, render_icon  # noqa: E402


def write_live(tmp_path, **kwargs):
    now = datetime.now(timezone.utc)
    defaults = dict(
        state=State.ACTIVE,
        session_start=now - timedelta(hours=1),
        resets_at=now + timedelta(hours=4),
        remaining=timedelta(hours=4),
        elapsed=timedelta(hours=1),
        utilization=22.0,
        source="oauth",
        confidence="authoritative",
    )
    defaults.update(kwargs)
    path = tmp_path / "live.json"
    live.publish(path, WindowView(**defaults), window_hours=5.0)
    return path


# --------------------------------------------------------------------------
# tray icon
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,progress,level",
    [
        (4 * 3600 + 12 * 60, 0.17, "normal"),
        (25 * 60, 0.92, "warning"),
        (3 * 60, 0.99, "critical"),
        (0, 1.0, "expired"),
        (None, 0.0, "unknown"),
    ],
)
def test_icon_renders_non_blank(seconds, progress, level):
    image = render_icon(seconds, progress, level)
    assert image.size == (ICON_SIZE, ICON_SIZE)
    assert image.mode == "RGBA"
    assert image.getbbox() is not None, "icon is entirely transparent"


def _pixels(image):
    """Every pixel as an RGBA tuple, without the deprecated getdata()."""
    px = image.load()
    width, height = image.size
    return [px[x, y] for y in range(height) for x in range(width)]


def test_icon_colour_tracks_urgency():
    """A glance at the tray should convey urgency without reading the number."""
    from windowsill.gui.tray import COLOURS

    def dominant(image, colour):
        return sum(1 for px in _pixels(image) if px == colour)

    calm = render_icon(4 * 3600, 0.2, "normal")
    urgent = render_icon(60, 0.99, "critical")

    assert dominant(calm, COLOURS["normal"]) > 0
    assert dominant(urgent, COLOURS["critical"]) > 0
    assert dominant(calm, COLOURS["critical"]) == 0


def test_icon_progress_changes_the_raster():
    early = render_icon(4 * 3600, 0.05, "normal")
    late = render_icon(4 * 3600, 0.95, "normal")
    assert _pixels(early) != _pixels(late)


def test_icon_label_is_ascii_only():
    """The fallback bitmap font renders non-ASCII as a tofu box."""
    import inspect

    from windowsill.gui import tray

    source = inspect.getsource(tray.render_icon)
    labels = [line for line in source.splitlines() if "label =" in line]
    assert labels, "render_icon no longer assigns a label"
    for line in labels:
        assert line.isascii(), f"non-ASCII tray label: {line.strip()}"


def test_icon_tolerates_out_of_range_progress():
    for progress in (-0.5, 1.5, float("inf")):
        try:
            image = render_icon(600, progress, "normal")
        except (ValueError, OverflowError):
            pytest.fail(f"progress={progress} crashed the renderer")
        assert image.size == (ICON_SIZE, ICON_SIZE)


# --------------------------------------------------------------------------
# detail panel
# --------------------------------------------------------------------------


def _tk_is_usable() -> bool:
    """Probe Tk once, thoroughly.

    Catching only TclError around `Tk()` is not enough: CI images ship broken
    Tcl installs (the Windows setup-python runner has an incomplete
    `tcl8.6` directory) where the failure surfaces from a later call and can
    hang the run. Building and tearing down a real root plus a Canvas is the
    only reliable check, and any exception at all means "skip".
    """
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        tk.Canvas(root, width=10, height=10).destroy()
        root.destroy()
        return True
    except Exception:
        return False


TK_USABLE = _tk_is_usable()


@pytest.fixture
def tk_root():
    if not TK_USABLE:
        pytest.skip("Tk is unavailable or broken in this environment")
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


def test_panel_renders_connected_state(tmp_path, tk_root):
    from windowsill.gui.panel import Panel

    path = write_live(tmp_path, remaining=timedelta(hours=4, minutes=12))
    panel = Panel(path, theme="dark")
    try:
        panel.root.update()
        assert "4:1" in panel.canvas.itemcget(panel.clock_text, "text")
        assert panel.status_label.cget("text") == "Active"
        assert panel.rows["source"].cget("text") == "oauth"
        assert "22.0%" in panel.rows["utilization"].cget("text")
        # The arc sweeps clockwise, so the extent is negative.
        assert float(panel.canvas.itemcget(panel.arc, "extent")) < 0
    finally:
        panel.root.destroy()


def test_panel_uses_the_claude_palette(tmp_path, tk_root):
    """Dark ground and the terracotta accent, not Tk's defaults."""
    from windowsill.gui.panel import Panel
    from windowsill.gui.theme import DARK

    path = write_live(tmp_path)
    panel = Panel(path, theme="dark")
    try:
        panel.root.update()
        assert panel.root.cget("bg") == DARK.bg
        assert panel.canvas.itemcget(panel.arc, "outline") == DARK.accent
        assert panel.status_label.cget("fg") == DARK.accent
    finally:
        panel.root.destroy()


def test_panel_light_theme(tmp_path, tk_root):
    from windowsill.gui.panel import Panel
    from windowsill.gui.theme import LIGHT

    panel = Panel(write_live(tmp_path), theme="light")
    try:
        panel.root.update()
        assert panel.root.cget("bg") == LIGHT.bg
    finally:
        panel.root.destroy()


def test_panel_arc_tracks_urgency_colour(tmp_path, tk_root):
    from windowsill.gui.panel import Panel
    from windowsill.gui.theme import DARK

    panel = Panel(write_live(tmp_path, remaining=timedelta(minutes=3)), theme="dark")
    try:
        panel.root.update()
        assert panel.canvas.itemcget(panel.arc, "outline") == DARK.critical
    finally:
        panel.root.destroy()


def test_panel_reports_a_missing_monitor(tmp_path, tk_root):
    from windowsill.gui.panel import Panel

    panel = Panel(tmp_path / "absent.json", theme="dark")
    try:
        panel.root.update()
        assert "Not connected" in panel.status_label.cget("text")
        assert panel.canvas.itemcget(panel.clock_text, "text") == "--:--:--"
        assert "sill tray" in panel.footer.cget("text")
    finally:
        panel.root.destroy()


def test_panel_survives_a_malformed_document(tmp_path, tk_root):
    from windowsill.gui.panel import Panel

    path = tmp_path / "live.json"
    path.write_text("{truncated", encoding="utf-8")
    panel = Panel(path, theme="dark")
    try:
        panel.root.update()  # must not raise
        assert "Not connected" in panel.status_label.cget("text")
    finally:
        panel.root.destroy()


def test_panel_shows_the_expired_state(tmp_path, tk_root):
    from windowsill.gui.panel import Panel

    path = write_live(tmp_path, remaining=timedelta(0), state=State.RESET_PENDING)
    panel = Panel(path, theme="dark")
    try:
        panel.root.update()
        assert panel.canvas.itemcget(panel.clock_text, "text") == "0:00:00"
        assert "lapsed" in panel.canvas.itemcget(panel.caption_text, "text")
    finally:
        panel.root.destroy()
