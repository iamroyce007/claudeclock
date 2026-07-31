"""State-machine tests.

These drive the tracker with a scripted fake source, so window transitions that
would take five hours in reality happen instantly and deterministically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from windowsill.config import Config
from windowsill.logging_setup import EventLog
from windowsill.sources import Confidence, Snapshot
from windowsill.tracker import State, WindowTracker


class FakeSource:
    """Replays a scripted list of snapshots, one per poll."""

    name = "fake"

    def __init__(self, script: list[Snapshot | None]) -> None:
        self.script = list(script)
        self.last_error: str | None = None
        self.closed = False

    def fetch(self) -> Snapshot | None:
        if not self.script:
            return None
        return self.script.pop(0)

    def close(self) -> None:
        self.closed = True


def make_snapshot(
    resets_at: datetime | None,
    *,
    confidence: str = Confidence.AUTHORITATIVE,
    source: str = "fake",
    utilization: float | None = None,
) -> Snapshot:
    return Snapshot(
        resets_at=resets_at,
        utilization=utilization,
        source=source,
        confidence=confidence,
    )


@pytest.fixture
def config(tmp_path):
    return Config(state_dir=tmp_path, alert_thresholds=(30, 10, 5), window_hours=5.0)


@pytest.fixture
def events(tmp_path):
    return EventLog(tmp_path / "events.jsonl")


def build(config, events, script, **kwargs):
    return WindowTracker(config, [FakeSource(script)], events, **kwargs)


# --------------------------------------------------------------------------
# window identity
# --------------------------------------------------------------------------


def test_subsecond_jitter_is_the_same_window(config, events):
    """The endpoint recomputes resets_at per call; that must not look new."""
    base = datetime.now(timezone.utc) + timedelta(hours=4)
    # Straddle a second boundary, which is exactly the case that used to break.
    script = [
        make_snapshot(base.replace(microsecond=900_000)),
        make_snapshot(base.replace(microsecond=100_000) + timedelta(seconds=1)),
        make_snapshot(base.replace(microsecond=500_000)),
    ]
    new_windows = []
    tracker = build(config, events, script, on_new_window=lambda v: new_windows.append(v))

    ids = []
    for _ in range(3):
        view = tracker.poll()
        ids.append(view.window_id)

    assert len(set(ids)) == 1, f"jitter produced multiple window ids: {ids}"
    assert new_windows == [], "jitter fired a spurious new-window callback"
    assert tracker.view.cycles_observed == 1


def test_genuinely_new_window_is_detected(config, events):
    now = datetime.now(timezone.utc)
    script = [
        make_snapshot(now + timedelta(minutes=1)),
        make_snapshot(now + timedelta(hours=5)),
    ]
    new_windows = []
    tracker = build(config, events, script, on_new_window=lambda v: new_windows.append(v))

    first = tracker.poll()
    second = tracker.poll()

    assert first.window_id != second.window_id
    assert len(new_windows) == 1
    assert tracker.view.cycles_observed == 2


def test_first_window_is_not_reported_as_a_reset(config, events):
    """Starting mid-window is not a reset; it is just where we came in."""
    new_windows = []
    tracker = build(
        config,
        events,
        [make_snapshot(datetime.now(timezone.utc) + timedelta(hours=2))],
        on_new_window=lambda v: new_windows.append(v),
    )
    tracker.poll()
    assert new_windows == []


def test_session_start_is_derived_from_reset(config, events):
    resets_at = datetime.now(timezone.utc) + timedelta(hours=3)
    tracker = build(config, events, [make_snapshot(resets_at)])
    view = tracker.poll()
    assert view.session_start == resets_at - timedelta(hours=5)


# --------------------------------------------------------------------------
# thresholds
# --------------------------------------------------------------------------


def test_threshold_fires_once_per_window(config, events):
    resets_at = datetime.now(timezone.utc) + timedelta(minutes=9)
    tracker = build(
        config,
        events,
        [make_snapshot(resets_at) for _ in range(4)],
    )
    fired: list[int] = []
    tracker.on_threshold = lambda minutes, view: fired.append(minutes)

    for _ in range(4):
        tracker.poll()

    assert fired == [10], f"expected a single 10m alert, got {fired}"


def test_late_start_does_not_burst_every_threshold(config, events):
    """Waking at 4 minutes left should alert once, not three times."""
    resets_at = datetime.now(timezone.utc) + timedelta(minutes=4)
    tracker = build(config, events, [make_snapshot(resets_at)])
    fired: list[int] = []
    tracker.on_threshold = lambda minutes, view: fired.append(minutes)

    tracker.poll()

    assert fired == [5], f"expected only the 5m alert, got {fired}"


def test_thresholds_reset_for_a_new_window(config, events):
    """The same threshold fires again once a different window crosses it.

    Both windows sit inside the 10-minute band (remaining in 5..10m) but are
    four minutes apart, comfortably outside the jitter tolerance, so they are
    genuinely distinct windows.
    """
    now = datetime.now(timezone.utc)
    script = [
        make_snapshot(now + timedelta(minutes=10)),
        make_snapshot(now + timedelta(minutes=6)),
    ]
    tracker = build(config, events, script)
    fired: list[int] = []
    tracker.on_threshold = lambda minutes, view: fired.append(minutes)

    tracker.poll()
    tracker.poll()

    assert fired == [10, 10]


def test_thresholds_survive_a_restart(config, events, tmp_path):
    """A restart mid-window must not re-fire an alert already delivered."""
    resets_at = datetime.now(timezone.utc) + timedelta(minutes=9)

    first = build(config, events, [make_snapshot(resets_at)])
    fired_a: list[int] = []
    first.on_threshold = lambda m, v: fired_a.append(m)
    first.poll()
    assert fired_a == [10]

    # New tracker, same state dir - simulates the process being restarted.
    second = build(config, events, [make_snapshot(resets_at)])
    fired_b: list[int] = []
    second.on_threshold = lambda m, v: fired_b.append(m)
    second.poll()

    assert fired_b == [], "restart re-fired an alert the user already saw"


# --------------------------------------------------------------------------
# reset / expiry
# --------------------------------------------------------------------------


def test_expired_window_enters_reset_pending(config, events):
    resets_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    resets: list = []
    tracker = build(
        config, events, [make_snapshot(resets_at)], on_reset=lambda v: resets.append(v)
    )
    view = tracker.poll()

    assert view.state == State.RESET_PENDING
    assert len(resets) == 1
    assert tracker.needs_trigger


def test_reset_announced_only_once(config, events):
    resets_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    resets: list = []
    tracker = build(
        config,
        events,
        [make_snapshot(resets_at) for _ in range(3)],
        on_reset=lambda v: resets.append(v),
    )
    for _ in range(3):
        tracker.poll()

    assert len(resets) == 1


def test_absent_window_means_reset_pending(config, events):
    """`five_hour: null` is the signal that nothing is currently open."""
    now = datetime.now(timezone.utc)
    tracker = build(
        config, events, [make_snapshot(now + timedelta(hours=1)), make_snapshot(None)]
    )
    tracker.poll()
    view = tracker.poll()

    assert view.state == State.RESET_PENDING
    assert view.remaining is None
    assert tracker.needs_trigger


# --------------------------------------------------------------------------
# source selection & degradation
# --------------------------------------------------------------------------


def test_higher_confidence_source_wins(config, events):
    now = datetime.now(timezone.utc)
    authoritative = make_snapshot(
        now + timedelta(hours=4), confidence=Confidence.AUTHORITATIVE, source="oauth"
    )
    inferred = make_snapshot(
        now + timedelta(hours=1), confidence=Confidence.INFERRED, source="local"
    )
    tracker = WindowTracker(
        config, [FakeSource([inferred]), FakeSource([authoritative])], events
    )
    view = tracker.poll()

    assert view.source == "oauth"
    assert view.confidence == Confidence.AUTHORITATIVE


def test_source_with_a_window_beats_one_without_at_equal_confidence(config, events):
    now = datetime.now(timezone.utc)
    empty = make_snapshot(None, confidence=Confidence.REPORTED, source="statusline")
    populated = make_snapshot(
        now + timedelta(hours=2), confidence=Confidence.REPORTED, source="other"
    )
    tracker = WindowTracker(config, [FakeSource([empty]), FakeSource([populated])], events)
    view = tracker.poll()

    assert view.source == "other"


def test_weaker_source_cannot_redefine_an_established_window(config, events):
    """Regression: local inference disagreeing must not fake a reset.

    Observed live - the endpoint failed one poll, local inference guessed a
    window 90 minutes off, and that was recorded as a whole new session.
    """
    now = datetime.now(timezone.utc)
    confirmed = make_snapshot(
        now + timedelta(hours=4), confidence=Confidence.AUTHORITATIVE, source="oauth"
    )
    # Same question, worse answer: an hour and a half adrift.
    guess = make_snapshot(
        now + timedelta(hours=2, minutes=30),
        confidence=Confidence.INFERRED,
        source="local",
    )
    new_windows = []
    tracker = build(
        config, events, [confirmed, guess],
        on_new_window=lambda v: new_windows.append(v),
    )

    first = tracker.poll()
    second = tracker.poll()

    assert new_windows == [], "a weaker source invented a new window"
    assert second.window_id == first.window_id
    assert second.resets_at == first.resets_at, "countdown jumped to the guess"
    assert second.stale and "coasting" in second.degraded_reason


def test_weaker_source_reporting_no_window_does_not_force_a_reset(config, events):
    """An offline local source must not look like 'the window closed'."""
    now = datetime.now(timezone.utc)
    confirmed = make_snapshot(
        now + timedelta(hours=3), confidence=Confidence.AUTHORITATIVE, source="oauth"
    )
    blind = make_snapshot(None, confidence=Confidence.INFERRED, source="local")

    resets: list = []
    tracker = build(
        config, events, [confirmed, blind], on_reset=lambda v: resets.append(v)
    )
    tracker.poll()
    view = tracker.poll()

    assert resets == [], "a blind fallback source triggered a reset"
    assert view.state == State.ACTIVE
    assert not tracker.needs_trigger


def test_authoritative_source_may_still_declare_a_new_window(config, events):
    """The guard must not block a genuine, server-confirmed reset."""
    now = datetime.now(timezone.utc)
    first = make_snapshot(
        now + timedelta(minutes=1), confidence=Confidence.AUTHORITATIVE, source="oauth"
    )
    second = make_snapshot(
        now + timedelta(hours=5), confidence=Confidence.AUTHORITATIVE, source="oauth"
    )
    new_windows = []
    tracker = build(
        config, events, [first, second], on_new_window=lambda v: new_windows.append(v)
    )
    tracker.poll()
    tracker.poll()

    assert len(new_windows) == 1


def test_better_source_corrects_rather_than_starting_a_cycle(config, events):
    """oauth recovering and overruling a local guess is a correction.

    The window never changed - only what we knew about it. Counting a cycle or
    firing a "new window" notification would be wrong on both counts.
    """
    now = datetime.now(timezone.utc)
    guess = make_snapshot(
        now + timedelta(hours=2), confidence=Confidence.INFERRED, source="local"
    )
    truth = make_snapshot(
        now + timedelta(hours=4), confidence=Confidence.AUTHORITATIVE, source="oauth"
    )
    new_windows = []
    tracker = build(
        config, events, [guess, truth], on_new_window=lambda v: new_windows.append(v)
    )

    tracker.poll()
    corrected = tracker.poll()

    assert new_windows == [], "a correction was announced as a new session"
    assert corrected.cycles_observed == 1, "a correction was counted as a cycle"
    assert corrected.resets_at == truth.resets_at, "countdown ignored the better source"
    assert corrected.confidence == Confidence.AUTHORITATIVE

    recorded = [e["event"] for e in _read_ledger(events)]
    assert "window_corrected" in recorded
    assert recorded.count("session_start") == 1


def _read_ledger(event_log):
    import json as _json

    if not event_log.path.exists():
        return []
    return [
        _json.loads(line)
        for line in event_log.path.read_text().splitlines()
        if line.strip()
    ]


def test_total_blackout_keeps_last_known_countdown(config, events):
    resets_at = datetime.now(timezone.utc) + timedelta(hours=2)
    tracker = build(config, events, [make_snapshot(resets_at), None])

    tracker.poll()
    view = tracker.poll()

    assert view.stale is True
    assert view.degraded_reason == "all sources unavailable"
    assert view.resets_at == resets_at, "countdown blanked instead of coasting"


def test_a_raising_source_does_not_kill_the_poll(config, events):
    class Exploding:
        name = "boom"

        def fetch(self):
            raise RuntimeError("kaboom")

        def close(self):
            pass

    resets_at = datetime.now(timezone.utc) + timedelta(hours=2)
    tracker = WindowTracker(
        config, [Exploding(), FakeSource([make_snapshot(resets_at)])], events
    )
    view = tracker.poll()

    assert view.resets_at == resets_at
    assert "error" in view.source_health["boom"]


# --------------------------------------------------------------------------
# trigger bookkeeping
# --------------------------------------------------------------------------


def test_note_trigger_records_outcome(config, events):
    tracker = build(config, events, [make_snapshot(None)])
    tracker.poll()
    tracker.note_trigger(ok=True, detail="ok")

    assert tracker.view.triggers_sent == 1
    assert tracker.view.last_trigger_ok is True
    assert tracker.view.last_trigger_at is not None


def test_restart_plus_jitter_is_not_a_new_window(config, events):
    """Regression: a restart must not turn sub-second jitter into a reset.

    The restored window id is the only anchor a fresh process has; if it is not
    rebuilt into a canonical reset instant, the first jittered reading lands on
    the other side of a second boundary and looks like a whole new window.
    """
    base = datetime.now(timezone.utc) + timedelta(hours=3)
    first = build(config, events, [make_snapshot(base.replace(microsecond=100_000))])
    first.poll()

    # Fresh tracker, same state dir; reading jitters back over the boundary.
    jittered = base.replace(microsecond=900_000) - timedelta(seconds=1)
    new_windows = []
    second = build(
        config, events, [make_snapshot(jittered)],
        on_new_window=lambda v: new_windows.append(v),
    )
    view = second.poll()

    assert new_windows == [], "restart + jitter fired a spurious new window"
    assert view.state != State.RESET_COMPLETE
    assert view.cycles_observed == 1


def test_restart_with_legacy_state_file_recovers(config, events, tmp_path):
    """State written before `canonical_reset_at` existed must still anchor."""
    resets_at = datetime.now(timezone.utc) + timedelta(hours=3)
    first = build(config, events, [make_snapshot(resets_at)])
    first.poll()

    # Simulate an older schema: drop the field a previous version never wrote.
    import json as _json

    data = _json.loads(config.state_file.read_text())
    del data["canonical_reset_at"]
    config.state_file.write_text(_json.dumps(data))

    second = build(config, events, [make_snapshot(resets_at)])
    assert second._canonical_reset_at is not None
    new_windows = []
    second.on_new_window = lambda v: new_windows.append(v)
    second.poll()
    assert new_windows == []


def test_state_persists_across_restart(config, events):
    resets_at = datetime.now(timezone.utc) + timedelta(hours=1)
    first = build(config, events, [make_snapshot(resets_at)])
    first.poll()
    first.note_trigger(ok=True)

    second = build(config, events, [make_snapshot(resets_at)])
    view = second.poll()

    assert view.triggers_sent == 1
    assert view.cycles_observed == 1, "restart double-counted the same window"


# --------------------------------------------------------------------------
# per-second ticking
# --------------------------------------------------------------------------


def test_tick_advances_the_countdown_without_polling(config, events):
    """Regression: the clock used to freeze between polls and then jump."""
    import time as _time

    source = FakeSource([make_snapshot(datetime.now(timezone.utc) + timedelta(hours=2))])
    tracker = WindowTracker(config, [source], events)
    tracker.poll()
    assert source.script == [], "fixture should be exhausted after one poll"

    first = tracker.tick().remaining
    _time.sleep(1.05)
    second = tracker.tick().remaining

    assert second < first, "countdown did not advance"
    assert 0.9 <= (first - second).total_seconds() <= 1.3


def test_tick_without_a_snapshot_is_safe(config, events):
    tracker = WindowTracker(config, [FakeSource([None])], events)
    view = tracker.tick()
    assert view.state == State.UNKNOWN


def test_tick_fires_a_threshold_at_the_boundary(config, events):
    """Alerts must land on time, not up to a poll interval late."""
    resets_at = datetime.now(timezone.utc) + timedelta(minutes=10, seconds=1)
    tracker = build(config, events, [make_snapshot(resets_at)])
    fired: list[int] = []
    tracker.on_threshold = lambda minutes, view: fired.append(minutes)

    # At 10m01s we are already past the 30-minute mark, so that fires at once.
    tracker.poll()
    assert fired == [30]

    import time as _time

    _time.sleep(1.2)  # cross the 10-minute boundary
    tracker.tick()

    assert fired == [30, 10], "threshold did not fire on the tick that crossed it"


def test_reset_complete_settles_back_to_active(config, events, monkeypatch):
    """The transient state used to stick permanently."""
    import windowsill.tracker as tracker_module

    now = datetime.now(timezone.utc)
    script = [
        make_snapshot(now + timedelta(minutes=1)),
        make_snapshot(now + timedelta(hours=4)),
    ]
    tracker = build(config, events, script)
    tracker.poll()
    assert tracker.poll().state == State.RESET_COMPLETE

    # Pretend the grace period has elapsed rather than sleeping through it.
    monkeypatch.setattr(tracker_module, "RESET_COMPLETE_GRACE", timedelta(seconds=0))
    assert tracker.tick().state == State.ACTIVE
