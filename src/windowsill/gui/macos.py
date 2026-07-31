"""macOS menu bar front-end, built on rumps (PyObjC).

The menu bar title *is* the countdown - `● 4:12:37` - refreshed every second,
and it keeps ticking while the dropdown is open. Clicking it reveals live
detail plus actions.

Threading
---------
rumps owns the main thread (it runs an NSApplication run loop), so the monitor
runs on background threads underneath it and a `rumps.Timer` pulls the current
view once a second. The detail window is launched as a *separate process*
because Tkinter also demands a main thread; trying to host both in one process
is the classic way these apps hang.

Keeping the dropdown live
-------------------------
While a menu is held open, AppKit switches the run loop into
`NSEventTrackingRunLoopMode`. `rumps.Timer` registers only in
`NSDefaultRunLoopMode`, so it stops firing the moment you click and the
dropdown freezes at whatever it said when it opened.

`LiveTimer` below fixes that by registering the same NSTimer in
`NSRunLoopCommonModes` *and* explicitly in `NSEventTrackingRunLoopMode`. The
explicit registration matters: "common modes" is a set that AppKit only
populates with the tracking mode once NSApplication is running, so relying on
it alone is fragile. Registering in both is unconditional, and CFRunLoop
de-duplicates, so the timer still fires exactly once per interval.
"""

from __future__ import annotations

import logging
import subprocess

import rumps
from Foundation import NSDate, NSRunLoop, NSRunLoopCommonModes, NSTimer

try:  # AppKit is present wherever rumps is, but degrade rather than crash.
    from AppKit import NSEventTrackingRunLoopMode
except ImportError:  # pragma: no cover - defensive
    NSEventTrackingRunLoopMode = "NSEventTrackingRunLoopMode"

from ..config import Config
from ..live import (
    LiveState,
    format_clock,
    format_local,
    menubar_title,
    read,
    status_glyph,
)

log = logging.getLogger("sill.gui.macos")


class LiveTimer:
    """A repeating main-thread timer that keeps firing while a menu is open.

    See the module docstring for why `rumps.Timer` is not enough. Registering
    in both the common modes and the event-tracking mode is what makes the
    countdown in the dropdown tick instead of freezing on click.
    """

    TRACKING_MODES = (NSRunLoopCommonModes, NSEventTrackingRunLoopMode)

    def __init__(self, callback, interval: float = 1.0) -> None:
        self._callback = callback
        self._interval = interval
        self._timer = None

    def start(self) -> None:
        if self._timer is not None:
            return
        self._timer = NSTimer.timerWithTimeInterval_repeats_block_(
            self._interval, True, lambda _timer: self._fire()
        )
        # Fire the first one promptly rather than after a full interval.
        self._timer.setFireDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))

        run_loop = NSRunLoop.currentRunLoop()
        for mode in self.TRACKING_MODES:
            run_loop.addTimer_forMode_(self._timer, mode)

    def _fire(self) -> None:
        try:
            self._callback()
        except Exception:
            # A raising timer callback would be swallowed by the ObjC bridge
            # and silently kill the tick; log it instead.
            log.exception("menu bar timer callback failed")

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None


class MenuBarApp(rumps.App):
    def __init__(self, config: Config, monitor=None) -> None:
        super().__init__("Claude", title="⏳ …", quit_button=None)
        self.config = config
        self.monitor = monitor
        self._panel_process: subprocess.Popen | None = None

        self.item_status = rumps.MenuItem("Starting…")
        self.item_remaining = rumps.MenuItem("")
        self.item_start = rumps.MenuItem("")
        self.item_reset = rumps.MenuItem("")
        self.item_used = rumps.MenuItem("")
        self.item_source = rumps.MenuItem("")

        self.menu = [
            self.item_status,
            None,
            self.item_remaining,
            self.item_start,
            self.item_reset,
            self.item_used,
            self.item_source,
            None,
            rumps.MenuItem("Open Detail Window", callback=self.open_panel),
            rumps.MenuItem("Send Re-arm Prompt Now", callback=self.send_trigger),
            None,
            rumps.MenuItem("Open Log Folder", callback=self.open_logs),
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        # Deliberately not rumps.Timer: that one stops firing while the menu is
        # open, which is exactly when the user is looking at the countdown.
        self.timer = LiveTimer(lambda: self.on_tick(None), interval=1.0)
        self.timer.start()

    # -- refresh ------------------------------------------------------------

    def on_tick(self, _sender) -> None:
        try:
            self._render(read(self.config.live_file))
        except Exception:
            log.exception("menu bar refresh failed")

    def _render(self, state: LiveState) -> None:
        # The menu bar text itself comes from a shared pure function so both
        # front-ends agree and it can be tested without an NSStatusItem.
        self.title = menubar_title(state, show_seconds=self.config.menubar_seconds)

        if not state.connected:
            self.item_status.title = f"⚠ {state.reason or 'not connected'}"
            for item in (
                self.item_remaining, self.item_start,
                self.item_reset, self.item_used, self.item_source,
            ):
                item.title = ""
            return

        seconds = state.remaining_seconds
        stale = bool(state.get("stale"))
        self.item_status.title = (
            f"{status_glyph(state.state, stale=stale)}  {state.state}"
            + ("  (degraded)" if stale else "")
        )
        self.item_remaining.title = f"Remaining      {format_clock(seconds)}"
        self.item_start.title = (
            f"Session start  {format_local(state.get('session_start'))}"
        )
        self.item_reset.title = f"Resets at      {format_local(state.get('resets_at'))}"

        used = state.get("utilization_percent")
        self.item_used.title = (
            f"Limit used     {float(used):.1f}%"
            if isinstance(used, (int, float))
            else "Limit used     —"
        )
        self.item_source.title = (
            f"Source         {state.get('source', '—')} "
            f"({state.get('confidence', '—')})"
        )

    # -- actions ------------------------------------------------------------

    def open_panel(self, _sender) -> None:
        """Launch the detail window in its own process (see module docstring)."""
        if self._panel_process is not None and self._panel_process.poll() is None:
            return  # already open
        from .app import spawn_panel

        self._panel_process = spawn_panel()
        if self._panel_process is None:
            rumps.notification(
                "Windowsill", "Could not open the detail window",
                "See the log for details.",
            )

    def send_trigger(self, _sender) -> None:
        if self.monitor is None:
            rumps.notification(
                "Windowsill", "Not available",
                "The monitor is not running in this process.",
            )
            return
        # Runs on a scheduler worker so the menu bar never blocks on the network.
        self.monitor.scheduler.add_job(self.monitor._rearm_job, "date")
        rumps.notification(
            "Windowsill", "Re-arm requested",
            f"Sending {self.config.trigger_prompt!r}…",
        )

    def open_logs(self, _sender) -> None:
        subprocess.Popen(["open", str(self.config.state_dir)])

    def quit_app(self, _sender) -> None:
        if self._panel_process is not None and self._panel_process.poll() is None:
            self._panel_process.terminate()
        if self.monitor is not None:
            self.monitor.shutdown()
        rumps.quit_application()


def run(config: Config, monitor=None) -> int:
    MenuBarApp(config, monitor=monitor).run()
    return 0
