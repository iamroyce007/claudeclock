"""The terminal dashboard.

Rich renderables are checked two ways here: the structural properties the
code sets directly (a panel's border style), and the text a render to a
fixed-width console produces. Both matter - the countdown's colour is the
only place the dashboard tells you how urgent the window is.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Console

from claudeclock.config import Config
from claudeclock.tracker import State, WindowView
from claudeclock.ui import Dashboard, plain_status_line, render_once


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


def render_to_text(config: Config, view: WindowView, width: int = 100) -> str:
    console = Console(width=width, record=True, file=open("/dev/null", "w"))
    render_once(config, view, console)
    return console.export_text()


# --------------------------------------------------------------------------
# countdown colour
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "minutes_left,expected",
    [
        (240, "green"),   # far from any threshold
        (20, "yellow"),   # inside the widest threshold
        (4, "red"),       # inside the narrowest
        (0, "magenta"),   # elapsed
    ],
)
def test_countdown_colour_follows_default_thresholds(tmp_path, minutes_left, expected):
    config = Config(state_dir=tmp_path, alert_thresholds=(30, 10, 5))
    panel = Dashboard(config)._countdown(make_view(remaining=timedelta(minutes=minutes_left)))
    assert panel.border_style == expected


def test_countdown_colour_honours_configured_thresholds(tmp_path):
    # With 60/20 configured, 25 minutes is a warning and 15 is critical. The
    # panel used to hardcode 30 and 5, so both of these read as "plenty of
    # time" while the notification for them had already fired.
    config = Config(state_dir=tmp_path, alert_thresholds=(60, 20))
    dashboard = Dashboard(config)

    assert dashboard._countdown(make_view(remaining=timedelta(minutes=25))).border_style == "yellow"
    assert dashboard._countdown(make_view(remaining=timedelta(minutes=15))).border_style == "red"
    assert dashboard._countdown(make_view(remaining=timedelta(minutes=90))).border_style == "green"


def test_countdown_without_a_window_says_so(tmp_path):
    config = Config(state_dir=tmp_path)
    text = render_to_text(config, make_view(state=State.RESET_PENDING, remaining=None))
    assert "no active window" in text


# --------------------------------------------------------------------------
# whole-dashboard render
# --------------------------------------------------------------------------


def test_render_includes_state_source_and_cycles(tmp_path):
    config = Config(state_dir=tmp_path)
    view = make_view(cycles_observed=3, source_health={"oauth": "ok", "local": "no data"})
    text = render_to_text(config, view)

    assert "Claude Usage Window" in text
    assert "Active" in text
    assert "oauth" in text
    assert "server-confirmed" in text
    assert "3" in text
    # An unhealthy source reports why, a healthy one does not need to.
    assert "no data" in text


def test_render_flags_a_stale_degraded_reading(tmp_path):
    config = Config(state_dir=tmp_path)
    view = make_view(stale=True, degraded_reason="all sources unavailable")
    text = render_to_text(config, view)

    assert "stale" in text
    assert "all sources unavailable" in text


def test_render_covers_every_state(tmp_path):
    """No state may fall through the style lookup and raise mid-paint."""
    config = Config(state_dir=tmp_path)
    for state in State:
        text = render_to_text(config, make_view(state=state))
        assert state.value in text


def test_weekly_limit_row_appears_only_when_reported(tmp_path):
    config = Config(state_dir=tmp_path)
    now = datetime.now(timezone.utc)

    without = render_to_text(config, make_view())
    assert "Weekly limit" not in without

    with_weekly = render_to_text(
        config,
        make_view(weekly_utilization=41.5, weekly_resets_at=now + timedelta(days=2)),
    )
    assert "Weekly limit" in with_weekly
    assert "41.5" in with_weekly


def test_plain_status_line_is_single_line(tmp_path):
    line = plain_status_line(make_view())
    assert "\n" not in line
    assert line.strip()
