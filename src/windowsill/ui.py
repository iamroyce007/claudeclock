"""The live terminal dashboard.

Rich `Live` render, repainted on a timer independent of the poll interval, so
the countdown ticks every second while the network is only touched once a
minute. Everything shown is derived from the tracker's `WindowView` - the UI
holds no state of its own.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .config import Config
from .sources import Confidence
from .tracker import State, WindowView

STATE_STYLES: dict[State, tuple[str, str]] = {
    State.UNKNOWN: ("grey62", "?"),
    State.ACTIVE: ("bold green", "●"),
    State.EXPIRING: ("bold yellow", "◐"),
    State.RESET_PENDING: ("bold magenta", "◌"),
    State.RESET_COMPLETE: ("bold cyan", "✓"),
}

CONFIDENCE_NOTE = {
    Confidence.AUTHORITATIVE: ("green", "server-confirmed"),
    Confidence.REPORTED: ("yellow", "reported by Claude Code"),
    Confidence.INFERRED: ("red", "locally inferred - approximate"),
}


def format_duration(delta: timedelta | None) -> str:
    if delta is None:
        return "--:--:--"
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_local(stamp: datetime | None, *, with_date: bool = False) -> str:
    if stamp is None:
        return "—"
    local = stamp.astimezone()
    pattern = "%Y-%m-%d %H:%M:%S %Z" if with_date else "%H:%M:%S %Z"
    return local.strftime(pattern)


def _relative(stamp: datetime | None) -> str:
    if stamp is None:
        return ""
    delta = datetime.now(timezone.utc) - stamp
    seconds = int(delta.total_seconds())
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


class Dashboard:
    """Renders a `WindowView` as a Rich renderable."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def render(self, view: WindowView) -> RenderableType:
        return Group(
            self._header(view),
            self._countdown(view),
            self._details(view),
            self._footer(view),
        )

    # -- sections -----------------------------------------------------------

    def _header(self, view: WindowView) -> RenderableType:
        style, glyph = STATE_STYLES.get(view.state, ("white", "•"))
        line = Text()
        line.append("  Claude Usage Window  ", style="bold white")
        line.append(f"{glyph} {view.state.value}", style=style)
        if view.stale:
            line.append("   [stale]", style="bold red")
        return Panel(
            Align.center(line),
            border_style=style.split()[-1] if " " in style else style,
            padding=(0, 1),
        )

    def _countdown(self, view: WindowView) -> RenderableType:
        if view.remaining is None:
            body: RenderableType = Align.center(
                Text(
                    "no active window — waiting for the next session to open",
                    style="italic grey70",
                )
            )
            return Panel(body, title="Time Remaining", border_style="grey50",
                         padding=(1, 2))

        remaining = view.remaining
        seconds_left = remaining.total_seconds()
        if seconds_left <= 0:
            colour = "magenta"
        elif seconds_left <= 5 * 60:
            colour = "red"
        elif seconds_left <= 30 * 60:
            colour = "yellow"
        else:
            colour = "green"

        clock = Text(format_duration(remaining), style=f"bold {colour}")

        bar = ProgressBar(
            total=100,
            completed=view.progress * 100,
            style="grey35",  # the unfilled track; Rich's default is near-invisible
            complete_style=colour,
            finished_style="magenta",
        )

        pct = Text()
        pct.append(f"{view.progress * 100:.1f}% elapsed", style="grey70")
        if view.utilization is not None:
            pct.append("   •   ", style="grey50")
            pct.append(f"{view.utilization:.1f}% of limit used", style="grey70")

        # A one-column expanding grid, so each element gets its own full-width
        # line. A plain Group lets the bar render at its natural width and the
        # caption then shares the row with it.
        body = Table.grid(expand=True)
        # ratio=1 makes the single column claim the full panel width, which is
        # what gives ProgressBar a max_width to expand into.
        body.add_column(justify="center", ratio=1)
        body.add_row(clock)
        body.add_row("")
        body.add_row(bar)
        body.add_row("")
        body.add_row(pct)

        return Panel(body, title="Time Remaining", border_style=colour,
                     padding=(1, 2))

    def _details(self, view: WindowView) -> RenderableType:
        table = Table.grid(padding=(0, 2))
        table.add_column(justify="right", style="grey62", no_wrap=True)
        table.add_column(justify="left", style="white")

        table.add_row("Session start", format_local(view.session_start, with_date=True))
        table.add_row("Resets at", format_local(view.resets_at, with_date=True))
        table.add_row("Elapsed", format_duration(view.elapsed))

        conf_style, conf_note = CONFIDENCE_NOTE.get(
            view.confidence, ("grey62", view.confidence)
        )
        origin = Text()
        origin.append(view.source, style="bold")
        origin.append(f"  ({conf_note})", style=conf_style)
        if view.observed_at:
            origin.append(f"  {_relative(view.observed_at)}", style="grey50")
        table.add_row("Source", origin)

        if view.weekly_utilization is not None:
            weekly = Text(f"{view.weekly_utilization:.1f}% used", style="white")
            if view.weekly_resets_at:
                weekly.append(
                    f"  resets {format_local(view.weekly_resets_at, with_date=True)}",
                    style="grey50",
                )
            table.add_row("Weekly limit", weekly)

        cycles = Text()
        cycles.append(str(view.cycles_observed), style="bold")
        cycles.append(" observed", style="grey62")
        if view.triggers_sent:
            cycles.append(f"   •   {view.triggers_sent} re-arm", style="grey62")
            cycles.append("s" if view.triggers_sent != 1 else "", style="grey62")
            if view.last_trigger_at:
                ok = view.last_trigger_ok
                mark = "✓" if ok else "✗"
                cycles.append(
                    f" (last {mark} {_relative(view.last_trigger_at)})",
                    style="green" if ok else "red",
                )
        table.add_row("Cycles", cycles)

        if view.degraded_reason:
            table.add_row("Degraded", Text(view.degraded_reason, style="bold red"))

        return Panel(table, title="Window", border_style="grey50", padding=(1, 2))

    def _footer(self, view: WindowView) -> RenderableType:
        health = Text()
        for index, (name, status) in enumerate(sorted(view.source_health.items())):
            if index:
                health.append("   ", style="grey50")
            ok = status == "ok"
            health.append("● ", style="green" if ok else "yellow")
            health.append(f"{name}", style="grey70")
            if not ok:
                health.append(f": {status}", style="grey50")
        if not view.source_health:
            health.append("no sources polled yet", style="grey50")

        hint = Text("  Ctrl-C to quit", style="dim grey50")
        return Panel(Group(health, hint), title="Sources",
                     border_style="grey35", padding=(0, 2))


def render_once(config: Config, view: WindowView, console: Console | None = None) -> None:
    """Print a single non-interactive snapshot (used by `sill status`)."""
    console = console or Console()
    console.print(Dashboard(config).render(view))


def plain_status_line(view: WindowView) -> str:
    """One-line summary for headless mode and log output."""
    parts = [f"state={view.state.value}"]
    if view.remaining is not None:
        parts.append(f"remaining={format_duration(view.remaining)}")
    if view.resets_at is not None:
        parts.append(f"resets_at={view.resets_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    if view.utilization is not None:
        parts.append(f"used={view.utilization:.1f}%")
    parts.append(f"source={view.source}/{view.confidence}")
    return "  ".join(parts)
