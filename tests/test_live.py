"""The live-state channel and the shared GUI formatting.

The menu bar / tray rendering is deliberately factored into pure functions so
it can be verified here without standing up an NSStatusItem or a tray icon.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from windowsill import live
from windowsill.tracker import State, WindowView


def make_view(**kwargs) -> WindowView:
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
    return WindowView(**defaults)


# --------------------------------------------------------------------------
# publish / read
# --------------------------------------------------------------------------


def test_roundtrip(tmp_path):
    path = tmp_path / "live.json"
    live.publish(path, make_view(), window_hours=5.0)
    state = live.read(path)

    assert state.connected
    assert state.state == "Active"
    assert state.get("source") == "oauth"
    assert 0.19 < state.progress < 0.21


def test_missing_file_reports_not_running(tmp_path):
    state = live.read(tmp_path / "absent.json")
    assert not state.connected
    assert "not running" in state.reason


def test_malformed_file_is_reported(tmp_path):
    path = tmp_path / "live.json"
    path.write_text("{not json", encoding="utf-8")
    state = live.read(path)
    assert not state.connected
    assert "unreadable" in state.reason


def test_stale_document_from_a_dead_writer(tmp_path):
    path = tmp_path / "live.json"
    live.publish(path, make_view(), window_hours=5.0)

    data = json.loads(path.read_text())
    data["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat()
    data["pid"] = 999_999  # almost certainly not a live process
    path.write_text(json.dumps(data), encoding="utf-8")

    state = live.read(path)
    assert not state.connected
    assert "not running" in state.reason


def test_stale_document_from_a_live_writer_says_stalled(tmp_path):
    path = tmp_path / "live.json"
    live.publish(path, make_view(), window_hours=5.0)

    data = json.loads(path.read_text())
    data["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat()
    data["pid"] = os.getpid()  # this process is very much alive
    path.write_text(json.dumps(data), encoding="utf-8")

    state = live.read(path)
    assert not state.connected
    assert "stalled" in state.reason


def test_countdown_is_extrapolated_from_document_age(tmp_path):
    """A reader rendering faster than 1 Hz must not see a stepping clock."""
    path = tmp_path / "live.json"
    live.publish(path, make_view(remaining=timedelta(seconds=600)), window_hours=5.0)

    data = json.loads(path.read_text())
    data["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=3)
    ).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")

    state = live.read(path)
    assert state.connected
    assert 596 <= state.remaining_seconds <= 598, "age was not subtracted"


def test_publish_is_atomic(tmp_path):
    """A reader must never observe a partially written document."""
    path = tmp_path / "live.json"
    for _ in range(50):
        live.publish(path, make_view(), window_hours=5.0)
        assert live.read(path).connected
    # No stray temp files left behind.
    assert [p.name for p in tmp_path.iterdir()] == ["live.json"]


def test_publish_survives_an_unwritable_directory(tmp_path):
    """Publishing is best-effort; it must never take the monitor down."""
    target = tmp_path / "nested" / "live.json"
    target.parent.mkdir()
    target.parent.chmod(0o500)
    try:
        live.publish(target, make_view(), window_hours=5.0)  # must not raise
    finally:
        target.parent.chmod(0o700)


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,full,compact",
    [
        (15157, "4:12:37", "4h12m"),
        (600, "0:10:00", "10m00s"),
        (45, "0:00:45", "45s"),
        (0, "0:00:00", "0s"),
        (None, "--:--", "--:--"),
    ],
)
def test_clock_formatting(seconds, full, compact):
    assert live.format_clock(seconds) == full
    assert live.format_clock(seconds, compact=True) == compact


def test_negative_clock_clamps_to_zero():
    assert live.format_clock(-30) == "0:00:00"


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (4 * 3600, "normal"),
        (31 * 60, "normal"),
        (30 * 60, "warning"),
        (6 * 60, "warning"),
        (5 * 60, "critical"),
        (30, "critical"),
        (0, "expired"),
        (-10, "expired"),
        (None, "unknown"),
    ],
)
def test_urgency_bands(seconds, expected):
    assert live.urgency(seconds) == expected


def test_menubar_title_when_connected(tmp_path):
    path = tmp_path / "live.json"
    live.publish(path, make_view(remaining=timedelta(hours=4, minutes=12)),
                 window_hours=5.0)
    title = live.menubar_title(live.read(path))

    assert title.startswith("●"), "healthy window should show the calm glyph"
    assert "4:1" in title, "default is a real per-second clock"
    assert "4h1" in live.menubar_title(live.read(path), show_seconds=False)


def test_menubar_title_turns_urgent(tmp_path):
    path = tmp_path / "live.json"
    live.publish(path, make_view(remaining=timedelta(minutes=3)), window_hours=5.0)
    assert live.menubar_title(live.read(path)).startswith("◔")


def test_menubar_title_when_expired(tmp_path):
    path = tmp_path / "live.json"
    live.publish(path, make_view(remaining=timedelta(seconds=0)), window_hours=5.0)
    assert "reset" in live.menubar_title(live.read(path))


def test_menubar_title_when_disconnected(tmp_path):
    assert live.menubar_title(live.read(tmp_path / "absent.json")) == "⏳ —"


def test_menubar_title_stays_short(tmp_path):
    """It shares the menu bar with the user's other items."""
    path = tmp_path / "live.json"
    for remaining in (timedelta(hours=4, minutes=59), timedelta(minutes=59),
                      timedelta(seconds=9), timedelta(0)):
        live.publish(path, make_view(remaining=remaining), window_hours=5.0)
        for show_seconds in (True, False):
            title = live.menubar_title(live.read(path), show_seconds=show_seconds)
            assert len(title) <= 10, f"menu bar title too wide: {title!r}"


def test_tray_tooltip_has_the_essentials(tmp_path):
    path = tmp_path / "live.json"
    live.publish(path, make_view(), window_hours=5.0)
    tooltip = live.tray_tooltip(live.read(path))

    assert "Active" in tooltip
    assert "remaining" in tooltip
    assert "22% used" in tooltip


def test_tray_tooltip_when_disconnected(tmp_path):
    tooltip = live.tray_tooltip(live.read(tmp_path / "absent.json"))
    assert "not running" in tooltip
