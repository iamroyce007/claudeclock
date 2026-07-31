"""Window-state sources.

A `Source` answers one question: *when does the current 5-hour window reset?*

Sources are ranked by trustworthiness and tried in order. Each returns a
`Snapshot` or `None`. `Snapshot.confidence` records how the answer was
obtained, so the UI can be honest about whether the countdown is authoritative
or merely inferred.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable


class Confidence(str):
    """How much to trust a snapshot's `resets_at`."""

    AUTHORITATIVE = "authoritative"  # server told us directly
    REPORTED = "reported"            # Claude Code told us, possibly stale
    INFERRED = "inferred"            # we derived it from observed local activity


@dataclass(frozen=True)
class Snapshot:
    """A point-in-time reading of the 5-hour window."""

    resets_at: datetime | None
    """UTC instant the window resets. None means no window is currently open."""

    utilization: float | None = None
    """Percent of the 5-hour limit consumed (0-100), if the source reports it."""

    weekly_utilization: float | None = None
    weekly_resets_at: datetime | None = None

    source: str = "unknown"
    confidence: str = Confidence.INFERRED
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_window(self) -> bool:
        return self.resets_at is not None

    def session_start(self, window_hours: float) -> datetime | None:
        """Infer when the window opened.

        The API gives us the reset instant, not the start. The window is a
        fixed length, so the start is simply `resets_at - window_hours`. This
        is exact whenever `resets_at` is authoritative.
        """
        if self.resets_at is None:
            return None
        return self.resets_at - timedelta(hours=window_hours)

    def remaining(self, now: datetime | None = None) -> timedelta | None:
        if self.resets_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return self.resets_at - now

    def window_id(self) -> str | None:
        """Stable identity for a window, used to de-duplicate alerts."""
        if self.resets_at is None:
            return None
        return str(int(self.resets_at.timestamp()))


@runtime_checkable
class Source(Protocol):
    name: str

    def fetch(self) -> Snapshot | None:
        """Return the current window state, or None if this source can't tell."""

    def close(self) -> None:
        ...


def parse_timestamp(value: Any) -> datetime | None:
    """Parse the several timestamp shapes the various surfaces use.

    Accepts ISO-8601 strings (with `Z` or an offset) and epoch seconds or
    milliseconds as int/float/numeric-string. Always returns tz-aware UTC.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return parse_timestamp(float(text))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    return None
