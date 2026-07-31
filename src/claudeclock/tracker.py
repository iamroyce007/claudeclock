"""The window state machine.

Owns the answer to "what is the state of my 5-hour window right now?", and the
transitions between states. Everything else in the package is plumbing around
this.

States
------
``UNKNOWN``        no source has answered yet
``ACTIVE``         a window is open and has time left
``EXPIRING``       open, but inside the smallest alert threshold
``RESET_PENDING``  the window's reset instant has passed, or no window is open
                   and we intend to re-arm one
``RESET_COMPLETE`` a *new* window was observed; a transient state that settles
                   back to ACTIVE on the next tick

Design notes
------------
Windows are identified by their reset instant (`window_id`). That makes alert
de-duplication trivial and survives restarts: the same window keeps the same id
across process boundaries, so a restart mid-window does not re-fire alerts the
user already saw.

Sources are consulted in configured order and the first usable answer wins,
except that a higher-confidence answer always supersedes a lower-confidence one
within the same tick. When an authoritative source speaks, its reset instant is
pushed into the local source as an anchor, so a later network outage degrades
to "keep counting down from a known-good boundary" rather than "re-guess".
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable

from .config import Config
from .logging_setup import EventLog
from .sources import Confidence, Snapshot, Source, parse_timestamp
from .sources.local import LocalInferenceSource, detect_clock_jump

log = logging.getLogger("cclock.tracker")

_CONFIDENCE_RANK = {
    Confidence.INFERRED: 0,
    Confidence.REPORTED: 1,
    Confidence.AUTHORITATIVE: 2,
}

# The usage endpoint's `resets_at` jitters by up to ~1s between calls (it is
# recomputed per request), and different sources disagree by a little more.
# Two readings within this tolerance are the same window. Without this, a
# reading that lands on the far side of a second boundary would look like a
# brand-new window and fire a spurious reset, notification and re-arm.
WINDOW_MATCH_TOLERANCE_SECONDS = 120.0

# How long the transient RESET_COMPLETE state stays on screen before settling
# back to ACTIVE. Long enough to notice, short enough not to mislead.
RESET_COMPLETE_GRACE = timedelta(seconds=30)


def _window_sort_key(window_id: str) -> tuple[int, float | str]:
    """Order window ids chronologically, tolerating anything unparseable."""
    try:
        return (0, float(window_id))
    except (TypeError, ValueError):
        return (1, str(window_id))


class State(str, Enum):
    UNKNOWN = "Unknown"
    ACTIVE = "Active"
    EXPIRING = "Expiring"
    RESET_PENDING = "Reset Pending"
    RESET_COMPLETE = "Reset Complete"


@dataclass
class WindowView:
    """Everything the UI needs to render, computed once per tick."""

    state: State = State.UNKNOWN
    session_start: datetime | None = None
    resets_at: datetime | None = None
    remaining: timedelta | None = None
    elapsed: timedelta | None = None
    utilization: float | None = None
    weekly_utilization: float | None = None
    weekly_resets_at: datetime | None = None
    source: str = "-"
    confidence: str = "-"
    observed_at: datetime | None = None
    window_id: str | None = None
    stale: bool = False
    degraded_reason: str | None = None
    cycles_observed: int = 0
    triggers_sent: int = 0
    last_trigger_at: datetime | None = None
    last_trigger_ok: bool | None = None
    source_health: dict[str, str] = field(default_factory=dict)

    @property
    def progress(self) -> float:
        """Fraction of the window elapsed, 0.0-1.0."""
        if self.elapsed is None or self.remaining is None:
            return 0.0
        total = self.elapsed.total_seconds() + self.remaining.total_seconds()
        if total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.elapsed.total_seconds() / total))


class WindowTracker:
    """Polls sources, maintains state, and emits transition callbacks."""

    def __init__(
        self,
        config: Config,
        sources: list[Source],
        event_log: EventLog,
        *,
        on_threshold: Callable[[int, WindowView], None] | None = None,
        on_reset: Callable[[WindowView], None] | None = None,
        on_new_window: Callable[[WindowView], None] | None = None,
    ) -> None:
        self.config = config
        self.sources = sources
        self.events = event_log
        self.on_threshold = on_threshold
        self.on_reset = on_reset
        self.on_new_window = on_new_window

        self._lock = threading.RLock()
        self.view = WindowView()

        self._current_window_id: str | None = None
        self._canonical_reset_at: datetime | None = None
        self._current_confidence: str = Confidence.INFERRED
        self._reset_complete_since: datetime | None = None
        self._fired_thresholds: set[tuple[str, int]] = set()
        self._last_snapshot: Snapshot | None = None
        self._reset_announced_for: set[str] = set()
        self._cycles = 0
        self._triggers = 0
        self._last_trigger_at: datetime | None = None
        self._last_trigger_ok: bool | None = None

        self._last_wall = time.time()
        self._last_monotonic = time.monotonic()

        self._local_source = next(
            (s for s in sources if isinstance(s, LocalInferenceSource)), None
        )

        self._restore()

    # -- persistence --------------------------------------------------------

    def _restore(self) -> None:
        """Reload cross-restart state so a restart mid-window is seamless."""
        path = self.config.state_file
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not restore state", extra={"error": str(exc)})
            return

        self._current_window_id = data.get("window_id")
        self._canonical_reset_at = parse_timestamp(data.get("canonical_reset_at"))
        if self._canonical_reset_at is None and self._current_window_id:
            # State written before `canonical_reset_at` existed, or truncated.
            # The window id *is* the reset instant in epoch seconds, so rebuild
            # the anchor from it - otherwise the first poll after a restart
            # re-derives it from a jittered reading and can spuriously look
            # like a brand-new window.
            self._canonical_reset_at = parse_timestamp(self._current_window_id)
        self._current_confidence = data.get("confidence") or Confidence.INFERRED
        # Seed the local source with the restored boundary, so a fresh process
        # that starts while the network is down coasts on the last confirmed
        # window instead of re-inferring a different one from transcripts.
        if self._local_source is not None and self._canonical_reset_at is not None:
            self._local_source.anchor = self._canonical_reset_at
        self._cycles = int(data.get("cycles_observed") or 0)
        self._triggers = int(data.get("triggers_sent") or 0)
        self._last_trigger_at = parse_timestamp(data.get("last_trigger_at"))
        self._reset_announced_for = set(data.get("reset_announced_for") or [])
        for item in data.get("fired_thresholds") or []:
            try:
                window_id, minutes = item
                self._fired_thresholds.add((str(window_id), int(minutes)))
            except (TypeError, ValueError):
                continue

        log.info(
            "restored previous state",
            extra={
                "window_id": self._current_window_id,
                "cycles": self._cycles,
                "triggers": self._triggers,
            },
        )

    def _persist(self) -> None:
        payload = {
            "window_id": self._current_window_id,
            "canonical_reset_at": self._canonical_reset_at.isoformat()
            if self._canonical_reset_at
            else None,
            "confidence": self._current_confidence,
            "cycles_observed": self._cycles,
            "triggers_sent": self._triggers,
            "last_trigger_at": self._last_trigger_at.isoformat()
            if self._last_trigger_at
            else None,
            # Both of these are bounded so the state file cannot grow forever.
            # Sort by window id (an epoch second) and keep the *newest*: a set
            # has no order, so slicing it directly could discard the current
            # window's entries and re-fire alerts the user already saw.
            "reset_announced_for": sorted(self._reset_announced_for, key=_window_sort_key)[-20:],
            "fired_thresholds": [
                [window_id, minutes]
                for window_id, minutes in sorted(
                    self._fired_thresholds, key=lambda item: (_window_sort_key(item[0]), item[1])
                )
            ][-100:],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            tmp = self.config.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.config.state_file)
        except OSError as exc:
            log.warning("could not persist state", extra={"error": str(exc)})

    # -- polling ------------------------------------------------------------

    def poll(self) -> WindowView:
        """Consult every source, fold the best answer into state, return view.

        Returns a *copy*: `self.view` is mutated in place on every poll, so
        handing out the live object would let a caller's "previous" view change
        under them, and would race the UI thread.
        """
        with self._lock:
            self._check_clock_jump()
            snapshot, health = self._best_snapshot()
            self._apply(snapshot, health)
            return WindowView(**asdict(self.view))

    def tick(self) -> WindowView:
        """Recompute the countdown against the current clock. No I/O.

        `poll()` is expensive and runs once a minute; this runs every second so
        the displayed clock actually counts down instead of freezing between
        polls and jumping. It also means threshold alerts fire within a second
        of the real boundary rather than up to a poll interval late.

        Only time-derived fields change - the underlying window reading is
        whatever the last poll established.
        """
        with self._lock:
            if self._last_snapshot is None:
                return WindowView(**asdict(self.view))

            now = datetime.now(timezone.utc)
            before = (len(self._fired_thresholds), len(self._reset_announced_for))

            self._recompute(self._last_snapshot, now, stale=self.view.stale)
            self._check_thresholds(now)
            self._check_expiry(self._last_snapshot, now)

            # Persist only when something durable actually changed; this runs
            # every second and must not churn the disk.
            if before != (len(self._fired_thresholds), len(self._reset_announced_for)):
                self._persist()

            return WindowView(**asdict(self.view))

    def _check_clock_jump(self) -> None:
        drift = detect_clock_jump(
            self._last_wall, self._last_monotonic, self.config.clock_jump_threshold
        )
        self._last_wall = time.time()
        self._last_monotonic = time.monotonic()
        if drift is None:
            return

        # Anything we inferred from elapsed time is now suspect. Drop the
        # anchor so the next poll re-derives the window from a real source.
        if self._local_source is not None:
            self._local_source.anchor = None
        self.events.record(
            "system_resume",
            drift_seconds=round(drift, 1),
            note="wall clock jumped; forcing re-sync",
        )

    def _best_snapshot(self) -> tuple[Snapshot | None, dict[str, str]]:
        best: Snapshot | None = None
        health: dict[str, str] = {}

        for source in self.sources:
            try:
                snapshot = source.fetch()
            except Exception as exc:  # a broken source must not kill the loop
                health[source.name] = f"error: {exc}"
                log.exception("source raised", extra={"source": source.name})
                continue

            if snapshot is None:
                health[source.name] = getattr(source, "last_error", None) or "no data"
                continue

            health[source.name] = "ok" if snapshot.has_window else "no active window"

            if best is None:
                best = snapshot
                continue

            better = _CONFIDENCE_RANK.get(snapshot.confidence, 0) > _CONFIDENCE_RANK.get(
                best.confidence, 0
            )
            # A source that can see an open window beats one that cannot, even
            # at equal confidence - "no window" is often just "not installed".
            fills_gap = snapshot.has_window and not best.has_window
            if better or fills_gap:
                best = snapshot

        return best, health

    # -- state transitions --------------------------------------------------

    def _apply(self, snapshot: Snapshot | None, health: dict[str, str]) -> None:
        now = datetime.now(timezone.utc)

        if snapshot is None:
            # Total blackout: keep counting down from the last known boundary
            # rather than blanking the display.
            self.view.stale = True
            self.view.degraded_reason = "all sources unavailable"
            self.view.source_health = health
            if self._last_snapshot is not None:
                self._recompute(self._last_snapshot, now, stale=True)
            return

        prior_confidence = self._current_confidence
        snapshot, coast_reason = self._canonicalize(snapshot)

        if snapshot.confidence == Confidence.AUTHORITATIVE and snapshot.has_window:
            if self._local_source is not None:
                self._local_source.anchor = snapshot.resets_at

        self._last_snapshot = snapshot
        self.view.stale = coast_reason is not None
        self.view.degraded_reason = coast_reason
        self.view.source_health = health

        new_id = snapshot.window_id()
        if new_id is not None and new_id != self._current_window_id:
            # A better source overruling a weaker one's guess is a *correction*,
            # not a new session: the window never changed, only our knowledge
            # of it. Counting it as a cycle would inflate the ledger and pop a
            # misleading "new window" notification.
            correction = _CONFIDENCE_RANK.get(snapshot.confidence, 0) > _CONFIDENCE_RANK.get(
                prior_confidence, -1
            )
            self._on_window_change(snapshot, now, new_id, correction=correction)

        self._recompute(snapshot, now, stale=coast_reason is not None)
        self._check_thresholds(now)
        self._check_expiry(snapshot, now)
        self._persist()

    def _canonicalize(self, snapshot: Snapshot) -> tuple[Snapshot, str | None]:
        """Reconcile a reading against the window we already believe in.

        Two distinct hazards, both of which would otherwise fake a reset:

        1. **Jitter.** The endpoint recomputes `resets_at` per request, so
           successive readings of the *same* window differ by a fraction of a
           second - enough to cross a second boundary and change the window id.
           Anything within `WINDOW_MATCH_TOLERANCE_SECONDS` is the same window,
           reported using the canonical instant so the countdown is steady too.

        2. **A weaker source disagreeing.** If the endpoint is unreachable and
           local inference guesses a window an hour off, that is not a new
           window - it is a worse answer to the same question. A lower-
           confidence source may never redefine a window an authoritative one
           established; it coasts on the known boundary instead.

        Returns the reconciled snapshot and, when coasting, a reason to show.
        """
        canonical = self._canonical_reset_at
        established = _CONFIDENCE_RANK.get(self._current_confidence, -1)
        incoming = _CONFIDENCE_RANK.get(snapshot.confidence, 0)
        now = datetime.now(timezone.utc)

        if canonical is not None and incoming < established and canonical > now:
            disagrees = snapshot.resets_at is None or (
                abs((snapshot.resets_at - canonical).total_seconds())
                > WINDOW_MATCH_TOLERANCE_SECONDS
            )
            if disagrees:
                log.debug(
                    "weaker source disagrees; coasting on the confirmed window",
                    extra={
                        "source": snapshot.source,
                        "confidence": snapshot.confidence,
                        "proposed": snapshot.resets_at.isoformat()
                        if snapshot.resets_at
                        else None,
                        "canonical": canonical.isoformat(),
                    },
                )
                return (
                    replace(snapshot, resets_at=canonical),
                    f"{snapshot.source} disagrees; coasting on last confirmed window",
                )

        if snapshot.resets_at is None:
            return snapshot, None

        if canonical is not None:
            drift = abs((snapshot.resets_at - canonical).total_seconds())
            if drift <= WINDOW_MATCH_TOLERANCE_SECONDS:
                if snapshot.resets_at != canonical:
                    log.log(5, "pinned jittering resets_at", extra={"drift": drift})
                    return replace(snapshot, resets_at=canonical), None
                return snapshot, None

        self._canonical_reset_at = snapshot.resets_at
        self._current_confidence = snapshot.confidence
        return snapshot, None

    def _on_window_change(
        self, snapshot: Snapshot, now: datetime, new_id: str, *, correction: bool = False
    ) -> None:
        previous = self._current_window_id
        self._current_window_id = new_id
        start = snapshot.session_start(self.config.window_hours)

        if correction and previous is not None:
            # Same window, better information. Re-point the countdown, but do
            # not count a cycle or announce a session that never started.
            log.info(
                "corrected window from a higher-confidence source",
                extra={
                    "previous_window_id": previous,
                    "window_id": new_id,
                    "source": snapshot.source,
                },
            )
            self.events.record(
                "window_corrected",
                window_id=new_id,
                previous_window_id=previous,
                resets_at=snapshot.resets_at,
                source=snapshot.source,
                confidence=snapshot.confidence,
            )
            # The corrected window is a different window as far as alerts are
            # concerned, so let its own thresholds fire.
            self._reset_announced_for.discard(previous)
            return

        self._cycles += 1
        self.events.record(
            "session_start",
            window_id=new_id,
            previous_window_id=previous,
            session_start=start,
            resets_at=snapshot.resets_at,
            source=snapshot.source,
            confidence=snapshot.confidence,
            cycle=self._cycles,
        )

        # Only a genuine succession counts as a completed reset; the first
        # window we ever see is just where we came in.
        if previous is not None:
            self.view.state = State.RESET_COMPLETE
            self._reset_complete_since = now
            if self.on_new_window:
                self._safe_callback(self.on_new_window, self.view)

    def _recompute(self, snapshot: Snapshot, now: datetime, *, stale: bool) -> None:
        view = self.view
        view.resets_at = snapshot.resets_at
        view.utilization = snapshot.utilization
        view.weekly_utilization = snapshot.weekly_utilization
        view.weekly_resets_at = snapshot.weekly_resets_at
        view.source = snapshot.source
        view.confidence = snapshot.confidence
        view.observed_at = snapshot.observed_at
        view.window_id = snapshot.window_id()
        view.cycles_observed = self._cycles
        view.triggers_sent = self._triggers
        view.last_trigger_at = self._last_trigger_at
        view.last_trigger_ok = self._last_trigger_ok
        view.stale = stale

        if snapshot.resets_at is None:
            view.session_start = None
            view.remaining = None
            view.elapsed = None
            if view.state not in (State.RESET_PENDING, State.RESET_COMPLETE):
                view.state = State.RESET_PENDING
            return

        start = snapshot.session_start(self.config.window_hours)
        view.session_start = start
        view.remaining = snapshot.resets_at - now
        view.elapsed = now - start if start else None

        remaining_s = view.remaining.total_seconds()
        if remaining_s <= 0:
            view.state = State.RESET_PENDING
        elif (
            view.state == State.RESET_COMPLETE
            and self._reset_complete_since is not None
            and (now - self._reset_complete_since) < RESET_COMPLETE_GRACE
        ):
            # Hold the transient state briefly so it is actually visible, then
            # let it settle. Without the deadline it would stick forever.
            pass
        elif remaining_s <= min(self.config.alert_thresholds) * 60:
            view.state = State.EXPIRING
        else:
            view.state = State.ACTIVE

    def _check_thresholds(self, now: datetime) -> None:
        view = self.view
        if view.remaining is None or view.window_id is None:
            return
        remaining_minutes = view.remaining.total_seconds() / 60.0
        if remaining_minutes <= 0:
            return

        # Every threshold we are at or past, most urgent first. With
        # thresholds 30/10/5 and 9 minutes left, that is [10, 30] - so the
        # alert to raise is "10 minutes remaining", not "30".
        crossed = sorted(t for t in self.config.alert_thresholds if remaining_minutes <= t)
        if not crossed:
            return

        target = crossed[0]
        if (view.window_id, target) in self._fired_thresholds:
            return

        # Mark every crossed threshold, not just the one we are firing. If the
        # machine slept through the 30- and 10-minute marks and woke at 4
        # minutes, the user gets the 5-minute alert only, not a burst of three.
        for threshold in crossed:
            self._fired_thresholds.add((view.window_id, threshold))

        self.events.record(
            "threshold_alert",
            window_id=view.window_id,
            threshold_minutes=target,
            remaining_minutes=round(remaining_minutes, 2),
            resets_at=view.resets_at,
        )
        if self.on_threshold:
            self._safe_callback(self.on_threshold, target, view)

    def _check_expiry(self, snapshot: Snapshot, now: datetime) -> None:
        """Announce the reset exactly once per window."""
        view = self.view
        expired = (
            snapshot.resets_at is not None and snapshot.resets_at <= now
        ) or (snapshot.resets_at is None and self._current_window_id is not None)
        if not expired:
            return

        marker = self._current_window_id or "unknown"
        if marker in self._reset_announced_for:
            view.state = State.RESET_PENDING
            return

        self._reset_announced_for.add(marker)
        view.state = State.RESET_PENDING
        self.events.record(
            "window_reset",
            window_id=marker,
            resets_at=snapshot.resets_at,
            source=snapshot.source,
        )
        if self.on_reset:
            self._safe_callback(self.on_reset, view)

    # -- trigger bookkeeping ------------------------------------------------

    def note_trigger(self, *, ok: bool, detail: str | None = None) -> None:
        with self._lock:
            self._triggers += 1
            self._last_trigger_at = datetime.now(timezone.utc)
            self._last_trigger_ok = ok
            self.view.triggers_sent = self._triggers
            self.view.last_trigger_at = self._last_trigger_at
            self.view.last_trigger_ok = ok
            if ok:
                # Force the next poll to look for the new window rather than
                # matching the one we just retired.
                self._reset_announced_for.discard(self._current_window_id or "")
            self._persist()
        log.info("trigger recorded", extra={"ok": ok, "detail": detail})

    @property
    def needs_trigger(self) -> bool:
        """True when the window has lapsed and nothing has re-opened one."""
        with self._lock:
            return self.view.state == State.RESET_PENDING

    def snapshot_view(self) -> WindowView:
        with self._lock:
            return WindowView(**asdict(self.view))

    @staticmethod
    def _safe_callback(callback: Callable[..., Any], *args: Any) -> None:
        try:
            callback(*args)
        except Exception:
            log.exception("callback failed")

    def close(self) -> None:
        for source in self.sources:
            try:
                source.close()
            except Exception:
                log.debug("source close failed", extra={"source": source.name})


def build_sources(config: Config) -> list[Source]:
    """Instantiate the configured sources, in priority order."""
    from .sources.oauth_usage import OAuthUsageSource
    from .sources.statusline import StatuslineSource

    built: list[Source] = []
    for name in config.sources:
        if name == "oauth":
            built.append(
                OAuthUsageSource(
                    explicit_token=config.oauth_token,
                    allow_refresh=config.allow_token_refresh,
                    backoff_min=config.backoff_min,
                    backoff_max=config.backoff_max,
                )
            )
        elif name == "statusline":
            built.append(StatuslineSource(config.statusline_file))
        elif name == "local":
            built.append(LocalInferenceSource(window_hours=config.window_hours))
    return built
