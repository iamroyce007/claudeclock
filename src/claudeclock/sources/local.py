"""Tertiary source: purely local inference.

Used only when neither official surface can answer - offline, signed out, or on
a plan whose limits the usage endpoint does not report. It is explicitly a
*fallback*, and everything it produces is marked `Confidence.INFERRED` so the
UI never presents a guess as fact.

The window start is inferred from the earliest first-request timestamp in the
current window, read from Claude Code's own local transcripts
(`~/.claude/projects/**/*.jsonl`). Each assistant message carries a `timestamp`;
the first one after the previous window closed marks the window's opening.

This is inference from local files the CLI writes for its own use - no
scraping, no undocumented network calls.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import Confidence, Snapshot, parse_timestamp

log = logging.getLogger("cclock.source.local")

TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

# Only inspect the tail of each transcript; these files can reach hundreds of
# MB and we only ever care about recent activity.
TAIL_BYTES = 8 * 1024 * 1024

# How far back to gather activity when chaining windows forward. Long enough
# to reach a genuine idle gap for almost any usage pattern.
LOOKBACK = timedelta(hours=24)


class LocalInferenceSource:
    """Infers the window from the timestamps Claude Code writes locally."""

    name = "local"

    def __init__(
        self,
        *,
        window_hours: float = 5.0,
        transcript_root: Path = TRANSCRIPT_ROOT,
    ) -> None:
        self.window_hours = window_hours
        self.transcript_root = transcript_root
        self.last_error: str | None = None
        # Set by the tracker when a higher-confidence source has spoken, so we
        # can anchor to a known-good window boundary instead of re-inferring.
        self.anchor: datetime | None = None

    def fetch(self) -> Snapshot | None:
        now = datetime.now(timezone.utc)
        window = timedelta(hours=self.window_hours)

        if self.anchor is not None and self.anchor > now:
            self.last_error = None
            return Snapshot(
                resets_at=self.anchor,
                source="local",
                confidence=Confidence.INFERRED,
                raw={"basis": "anchor"},
            )

        first_activity = self._current_window_start(now, window)
        if first_activity is None:
            self.last_error = "no recent local Claude activity found"
            log.log(5, "local inference found no activity in window")
            return Snapshot(
                resets_at=None,
                source="local",
                confidence=Confidence.INFERRED,
                raw={"basis": "no-activity"},
            )

        self.last_error = None
        resets_at = first_activity + window
        log.debug(
            "inferred window from local transcripts",
            extra={
                "first_activity": first_activity.isoformat(),
                "resets_at": resets_at.isoformat(),
            },
        )
        return Snapshot(
            resets_at=resets_at,
            source="local",
            confidence=Confidence.INFERRED,
            raw={"basis": "transcript", "first_activity": first_activity.isoformat()},
        )

    # -- transcript scanning ------------------------------------------------

    def _current_window_start(
        self, now: datetime, window: timedelta
    ) -> datetime | None:
        """Chain windows forward through activity to find the current one.

        A window opens with the first request after the previous one elapsed,
        so the start cannot be read off a rolling lookback: for someone active
        continuously, "earliest activity in the last 5 hours" is just
        "now - 5 hours", which drifts further from the truth the more you use
        Claude. (Measured against the server's own figure, that naive approach
        was 90 minutes out.)

        Instead, walk every timestamp in order from well before the current
        window: the first one opens a window, and any timestamp landing past
        that window's end opens the next. The last window opened is the
        current one.
        """
        stamps = sorted(self._activity_since(now - LOOKBACK))
        if not stamps:
            return None

        start = stamps[0]
        for stamp in stamps:
            if stamp >= start + window:
                start = stamp

        # If that window has already elapsed, no window is currently open -
        # the next request will open one.
        if start + window <= now:
            return None
        return start

    def _activity_since(self, cutoff: datetime) -> list[datetime]:
        """Every transcript timestamp at or after `cutoff`."""
        if not self.transcript_root.exists():
            return []

        found: list[datetime] = []
        cutoff_epoch = cutoff.timestamp()

        for path in self._candidate_files(cutoff_epoch):
            for stamp in self._timestamps_in(path):
                if stamp >= cutoff:
                    found.append(stamp)
        return found

    def _candidate_files(self, cutoff_epoch: float) -> list[Path]:
        """Transcripts modified since the window opened."""
        candidates: list[Path] = []
        try:
            for path in self.transcript_root.rglob("*.jsonl"):
                try:
                    if path.stat().st_mtime >= cutoff_epoch:
                        candidates.append(path)
                except OSError:
                    continue
        except OSError as exc:
            log.debug("transcript scan failed", extra={"error": str(exc)})
        return candidates

    @staticmethod
    def _timestamps_in(path: Path) -> list[datetime]:
        """Timestamps from the tail of one transcript."""
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > TAIL_BYTES:
                    handle.seek(size - TAIL_BYTES)
                    handle.readline()  # discard the partial line we landed in
                blob = handle.read().decode("utf-8", errors="replace")
        except OSError as exc:
            log.log(5, "transcript unreadable", extra={"path": str(path), "error": str(exc)})
            return []

        stamps: list[datetime] = []
        for line in blob.splitlines():
            line = line.strip()
            if not line or '"timestamp"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            # Only real API turns mark the start of a usage window.
            if entry.get("type") not in ("user", "assistant"):
                continue
            if entry.get("isSidechain") and entry.get("type") == "user":
                continue
            stamp = parse_timestamp(entry.get("timestamp"))
            if stamp is not None:
                stamps.append(stamp)
        return stamps

    def close(self) -> None:
        return None


def detect_clock_jump(
    last_wall: float, last_monotonic: float, threshold: float
) -> float | None:
    """Return the size of a wall-clock jump, or None if the clocks agree.

    Wall time and monotonic time advance together while the machine is awake.
    After a suspend/resume, wall time has moved further than monotonic time
    (on platforms where monotonic pauses during sleep), and an operator
    changing the system clock shows up the same way. The drift is the interval
    during which our timers were effectively not running, so any large value
    means "stop trusting elapsed time and re-sync now".
    """
    wall_delta = time.time() - last_wall
    monotonic_delta = time.monotonic() - last_monotonic
    drift = wall_delta - monotonic_delta
    if abs(drift) >= threshold:
        return drift
    return None
