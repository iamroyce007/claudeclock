"""The long-running monitor.

Wires the tracker, notifier, trigger and dashboard onto an APScheduler
background scheduler, then blocks until interrupted.

Three jobs run on independent cadences:

* **poll** (every `poll_interval`) - refresh window state from the sources
* **paint** (every `ui_refresh`) - repaint the countdown, no I/O
* **rearm** (one-shot, scheduled on demand) - send the re-arm prompt after the
  window lapses

Keeping paint off the poll cadence is what lets the clock tick every second
while the network is touched once a minute.
"""

from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from rich.console import Console
from rich.live import Live

from . import live as live_state
from .config import Config
from .logging_setup import EventLog
from .notify import Notification, Notifier
from .tracker import State, WindowTracker, WindowView, build_sources
from .trigger import dry_run_description, send_trigger
from .ui import Dashboard, format_duration, format_local, plain_status_line

log = logging.getLogger("cclock.monitor")

REARM_JOB_ID = "rearm"


class Monitor:
    def __init__(
        self,
        config: Config,
        *,
        headless: bool = False,
        sources: list | None = None,
    ) -> None:
        self.config = config
        self.headless = headless
        self.console = Console()
        self.events = EventLog(config.event_log_file)
        self.notifier = Notifier(config)
        self.dashboard = Dashboard(config)

        self.tracker = WindowTracker(
            config,
            sources if sources is not None else build_sources(config),
            self.events,
            on_threshold=self._on_threshold,
            on_reset=self._on_reset,
            on_new_window=self._on_new_window,
        )

        self.scheduler = BackgroundScheduler(
            timezone="UTC",
            job_defaults={
                "coalesce": True,        # after a sleep, run once, not N times
                "max_instances": 1,      # never overlap a slow poll with itself
                "misfire_grace_time": 300,
            },
        )
        self._stop = threading.Event()
        self._rearm_lock = threading.Lock()
        self._rearm_in_flight = False
        self._on_tick = None
        self._bg_thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------

    def _startup(self) -> None:
        """Everything needed before a front-end can render. Idempotent-ish."""
        self.config.ensure_state_dir()
        self.notifier.start()

        self.events.record(
            "monitor_start",
            sources=list(self.config.sources),
            window_hours=self.config.window_hours,
            auto_trigger=self.config.auto_trigger,
            poll_interval=self.config.poll_interval,
        )
        log.info(
            "monitor starting",
            extra={
                "state_dir": str(self.config.state_dir),
                "trigger": dry_run_description(self.config)
                if self.config.auto_trigger
                else "disabled",
            },
        )

        self._install_signal_handlers()

        # One synchronous poll before the UI opens, so the first frame is real
        # data rather than an empty skeleton.
        try:
            self.tracker.poll()
        except Exception:
            log.exception("initial poll failed")

        self.scheduler.add_job(
            self._poll_job,
            "interval",
            seconds=self.config.poll_interval,
            id="poll",
            next_run_time=datetime.now(timezone.utc)
            + timedelta(seconds=self.config.poll_interval),
        )
        self.scheduler.start()
        self.publish(self.tracker.tick())

    def start_background(self, on_tick=None) -> None:
        """Run the monitor on background threads, for a GUI to sit on top of.

        The menu bar / tray toolkits demand the main thread, so the tick loop
        moves off it. `on_tick` is invoked once a second with the current view,
        which is how the Windows tray icon gets repainted.
        """
        self._startup()
        self._on_tick = on_tick
        self._bg_thread = threading.Thread(
            target=self._background_loop, name="cclock-monitor", daemon=True
        )
        self._bg_thread.start()

    def _background_loop(self) -> None:
        while not self._stop.is_set():
            try:
                view = self.tracker.tick()
                self.publish(view)
                self._maybe_rearm(view)
                if self._on_tick is not None:
                    self._on_tick(view)
            except Exception:
                log.exception("background tick failed")
            self._stop.wait(self.config.ui_refresh)

    def run(self) -> int:
        self._startup()
        try:
            if self.headless:
                self._run_headless()
            else:
                self._run_live()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):  # type: ignore[no-untyped-def]
            log.info("received signal, shutting down", extra={"signal": signum})
            self._stop.set()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Not on the main thread, or unsupported on this platform.
                pass

    def _run_live(self) -> None:
        with Live(
            self.dashboard.render(self.tracker.snapshot_view()),
            console=self.console,
            refresh_per_second=max(1, int(1 / self.config.ui_refresh)),
            screen=False,
            transient=False,
        ) as live:
            while not self._stop.is_set():
                # tick(), not snapshot_view(): recompute the countdown against
                # the current clock so it actually counts down between polls.
                view = self.tracker.tick()
                live.update(self.dashboard.render(view))
                self.publish(view)
                self._maybe_rearm(view)
                self._stop.wait(self.config.ui_refresh)

    def _run_headless(self) -> None:
        log.info("running headless; watch the log or events.jsonl")
        last_key = None
        while not self._stop.is_set():
            view = self.tracker.tick()
            self.publish(view)
            self._maybe_rearm(view)

            # Tick every second so alerts land on time, but log only on a
            # meaningful change - the raw status line contains a seconds-
            # resolution countdown and would otherwise spam a line per second.
            key = (
                view.state,
                view.source,
                view.stale,
                int(view.remaining.total_seconds() // 60) if view.remaining else None,
            )
            if key != last_key:
                log.info("status: %s", plain_status_line(view))
                last_key = key

            self._stop.wait(self.config.ui_refresh)

    def shutdown(self) -> None:
        log.info("shutting down")
        self._stop.set()
        try:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
        except Exception:
            log.debug("scheduler shutdown raised", exc_info=True)
        self.events.record("monitor_stop", cycles=self.tracker.view.cycles_observed)
        self.tracker.close()
        self.notifier.stop()

    def publish(self, view: WindowView) -> None:
        """Broadcast the current view to any attached GUI front-end."""
        live_state.publish(
            self.config.live_file, view, window_hours=self.config.window_hours
        )

    # -- jobs ---------------------------------------------------------------

    def _poll_job(self) -> None:
        try:
            view = self.tracker.poll()
            log.debug("polled", extra={"status": plain_status_line(view)})
        except Exception:
            log.exception("poll job failed")

    def _maybe_rearm(self, view: WindowView) -> None:
        """Schedule the re-arm prompt once the window has lapsed."""
        if not self.config.auto_trigger:
            return
        if view.state != State.RESET_PENDING:
            return

        with self._rearm_lock:
            if self._rearm_in_flight:
                return
            if self.scheduler.get_job(REARM_JOB_ID) is not None:
                return
            self._rearm_in_flight = True

        run_at = datetime.now(timezone.utc) + timedelta(seconds=self.config.trigger_delay)
        log.info("scheduling re-arm", extra={"at": run_at.isoformat()})
        self.scheduler.add_job(
            self._rearm_job,
            "date",
            run_date=run_at,
            id=REARM_JOB_ID,
            replace_existing=True,
        )

    def _rearm_job(self) -> None:
        try:
            self.events.record(
                "trigger_attempt",
                prompt=self.config.trigger_prompt,
                command=dry_run_description(self.config),
            )
            result = send_trigger(self.config)
            self.events.record(
                "trigger_result",
                ok=result.ok,
                attempts=result.attempts,
                duration_seconds=round(result.duration, 2),
                detail=result.detail,
                session_id=result.session_id,
            )
            self.tracker.note_trigger(ok=result.ok, detail=result.detail)

            if result.ok:
                self.notifier.send(
                    Notification(
                        title="New session started",
                        message=(
                            f"Sent {self.config.trigger_prompt!r} to open the next "
                            "5-hour window."
                        ),
                        level="success",
                        fields={"attempts": result.attempts,
                                "session": result.session_id or "n/a"},
                    )
                )
            else:
                self.notifier.send(
                    Notification(
                        title="Could not start new session",
                        message=result.detail,
                        level="error",
                        fields={"attempts": result.attempts},
                    )
                )

            # Poll straight away so the new window's real reset time lands in
            # the UI without waiting out the poll interval.
            self.tracker.poll()
        except Exception:
            log.exception("re-arm job crashed")
        finally:
            with self._rearm_lock:
                self._rearm_in_flight = False

    # -- tracker callbacks --------------------------------------------------

    def _on_threshold(self, minutes: int, view: WindowView) -> None:
        self.notifier.send(
            Notification(
                title=f"{minutes} minutes remaining",
                message=(
                    f"Your 5-hour window resets at "
                    f"{format_local(view.resets_at, with_date=True)}."
                ),
                level="warning",
                fields={
                    "remaining": format_duration(view.remaining),
                    "used": f"{view.utilization:.1f}%"
                    if view.utilization is not None
                    else "unknown",
                },
            )
        )

    def _on_reset(self, view: WindowView) -> None:
        message = "Your 5-hour usage window has reset."
        if self.config.auto_trigger:
            message += (
                f" Sending {self.config.trigger_prompt!r} in "
                f"{self.config.trigger_delay:.0f}s to open the next one."
            )
        self.notifier.send(
            Notification(title="Usage window reset", message=message, level="info")
        )

    def _on_new_window(self, view: WindowView) -> None:
        self.notifier.send(
            Notification(
                title="New 5-hour window active",
                message=(
                    f"Started {format_local(view.session_start, with_date=True)}, "
                    f"resets {format_local(view.resets_at, with_date=True)}."
                ),
                level="success",
                fields={"cycle": view.cycles_observed},
            )
        )
