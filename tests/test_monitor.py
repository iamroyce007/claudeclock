"""End-to-end orchestration: reset detection through to the re-arm subprocess.

Drives a real `Monitor` (real scheduler, real trigger subprocess, real event
ledger) with a scripted source, so the full five-hour cycle is exercised in
milliseconds.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone

import pytest

from claudeclock.config import Config
from claudeclock.monitor import REARM_JOB_ID, Monitor
from claudeclock.tracker import State

from .test_tracker import FakeSource, make_snapshot


@pytest.fixture
def fake_claude(tmp_path):
    """A stub CLI that records each invocation and succeeds."""
    calls = tmp_path / "calls.txt"
    script = tmp_path / "fake_claude.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json, sys
            with open({str(calls)!r}, "a") as fh:
                fh.write(" ".join(sys.argv[1:]) + "\\n")
            print(json.dumps({{"session_id": "new-session"}}))
            """
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"', calls


def make_config(tmp_path, command, **kwargs):
    defaults = dict(
        state_dir=tmp_path,
        trigger_command=f"{command} -p {{prompt}}",
        trigger_model=None,
        trigger_delay=0.0,
        trigger_timeout=30.0,
        trigger_max_retries=0,
        desktop_notifications=False,
        poll_interval=3600.0,   # we drive polls by hand
        ui_refresh=0.05,
        backoff_min=0.01,
        backoff_max=0.02,
    )
    defaults.update(kwargs)
    return Config(**defaults)


def read_events(config):
    if not config.event_log_file.exists():
        return []
    return [
        json.loads(line)
        for line in config.event_log_file.read_text().splitlines()
        if line.strip()
    ]


def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------


def test_lapsed_window_triggers_a_rearm(tmp_path, fake_claude):
    """The core loop: window expires -> prompt sent -> new window observed."""
    command, calls = fake_claude
    config = make_config(tmp_path, command, trigger_prompt="Hi")

    now = datetime.now(timezone.utc)
    expired = make_snapshot(now - timedelta(seconds=1))
    fresh = make_snapshot(now + timedelta(hours=5))
    source = FakeSource([expired, expired, fresh, fresh, fresh])

    monitor = Monitor(config, headless=True, sources=[source])
    monitor.scheduler.start()
    try:
        view = monitor.tracker.poll()
        assert view.state == State.RESET_PENDING

        monitor._maybe_rearm(view)
        assert wait_for(lambda: calls.exists()), "re-arm prompt was never sent"

        assert calls.read_text().strip().endswith("Hi")
        assert wait_for(lambda: monitor.tracker.view.triggers_sent == 1)
    finally:
        monitor.scheduler.shutdown(wait=True)
        monitor.notifier.stop()

    events = {e["event"] for e in read_events(config)}
    assert {"window_reset", "trigger_attempt", "trigger_result"} <= events

    result = next(e for e in read_events(config) if e["event"] == "trigger_result")
    assert result["ok"] is True
    assert result["session_id"] == "new-session"


def test_rearm_is_not_scheduled_twice(tmp_path, fake_claude):
    """Repeated RESET_PENDING ticks must not stack up duplicate prompts."""
    command, calls = fake_claude
    config = make_config(tmp_path, command, trigger_delay=5.0)

    expired = make_snapshot(datetime.now(timezone.utc) - timedelta(seconds=1))
    source = FakeSource([expired] * 10)

    monitor = Monitor(config, headless=True, sources=[source])
    monitor.scheduler.start()
    try:
        view = monitor.tracker.poll()
        for _ in range(10):
            monitor._maybe_rearm(view)

        jobs = [j for j in monitor.scheduler.get_jobs() if j.id == REARM_JOB_ID]
        assert len(jobs) == 1, f"scheduled {len(jobs)} re-arm jobs, expected 1"
    finally:
        monitor.scheduler.shutdown(wait=False)
        monitor.notifier.stop()

    assert not calls.exists(), "prompt fired despite the configured delay"


def test_no_rearm_while_the_window_is_active(tmp_path, fake_claude):
    command, calls = fake_claude
    config = make_config(tmp_path, command)

    active = make_snapshot(datetime.now(timezone.utc) + timedelta(hours=3))
    monitor = Monitor(config, headless=True, sources=[FakeSource([active])])
    monitor.scheduler.start()
    try:
        view = monitor.tracker.poll()
        assert view.state == State.ACTIVE
        monitor._maybe_rearm(view)
        time.sleep(0.3)

        assert monitor.scheduler.get_job(REARM_JOB_ID) is None
    finally:
        monitor.scheduler.shutdown(wait=False)
        monitor.notifier.stop()

    assert not calls.exists()


def test_auto_trigger_disabled_never_sends(tmp_path, fake_claude):
    command, calls = fake_claude
    config = make_config(tmp_path, command, auto_trigger=False)

    expired = make_snapshot(datetime.now(timezone.utc) - timedelta(seconds=1))
    monitor = Monitor(config, headless=True, sources=[FakeSource([expired] * 3)])
    monitor.scheduler.start()
    try:
        view = monitor.tracker.poll()
        assert view.state == State.RESET_PENDING
        monitor._maybe_rearm(view)
        time.sleep(0.3)

        assert monitor.scheduler.get_job(REARM_JOB_ID) is None
    finally:
        monitor.scheduler.shutdown(wait=False)
        monitor.notifier.stop()

    assert not calls.exists(), "observe-only mode sent a prompt"


def test_failed_rearm_is_recorded_not_raised(tmp_path):
    """A broken CLI must degrade to a logged failure, not crash the monitor."""
    failing = tmp_path / "failing.py"
    failing.write_text("import sys; sys.stderr.write('boom'); sys.exit(1)")
    command = f'"{sys.executable}" "{failing}"'
    config = make_config(tmp_path, command)

    expired = make_snapshot(datetime.now(timezone.utc) - timedelta(seconds=1))
    monitor = Monitor(config, headless=True, sources=[FakeSource([expired] * 5)])
    monitor.scheduler.start()
    try:
        monitor.tracker.poll()
        monitor._rearm_job()
    finally:
        monitor.scheduler.shutdown(wait=False)
        monitor.notifier.stop()

    result = next(e for e in read_events(config) if e["event"] == "trigger_result")
    assert result["ok"] is False
    assert monitor.tracker.view.last_trigger_ok is False


def test_threshold_notification_reaches_the_notifier(tmp_path, fake_claude):
    command, _ = fake_claude
    config = make_config(tmp_path, command, alert_thresholds=(30, 10, 5))

    nearly_done = make_snapshot(datetime.now(timezone.utc) + timedelta(minutes=4))
    monitor = Monitor(config, headless=True, sources=[FakeSource([nearly_done])])

    sent = []
    monitor.notifier.send = lambda note: sent.append(note)
    try:
        monitor.tracker.poll()
    finally:
        monitor.notifier.stop()

    assert len(sent) == 1
    assert "5 minutes remaining" in sent[0].title
    assert sent[0].level == "warning"


# --------------------------------------------------------------------------
# adaptive polling
# --------------------------------------------------------------------------


def test_poll_backs_off_when_the_window_has_hours_left(tmp_path, fake_claude):
    """resets_at is fixed within a window, so there is nothing to re-fetch.

    Regression: a fixed 60s cadence trips the endpoint's rate limit, and every
    retry re-trips it, so the app gets stuck on inferred data and stops
    matching the real Claude limit.
    """
    command, _ = fake_claude
    config = make_config(tmp_path, command, poll_interval=600.0)

    active = make_snapshot(datetime.now(timezone.utc) + timedelta(hours=4))
    monitor = Monitor(config, headless=True, sources=[FakeSource([active])])
    try:
        monitor.tracker.poll()
        assert monitor._next_poll_delay() >= 600.0
    finally:
        monitor.notifier.stop()


def test_poll_tightens_near_the_boundary(tmp_path, fake_claude):
    command, _ = fake_claude
    config = make_config(tmp_path, command, poll_interval=600.0)

    nearly = make_snapshot(datetime.now(timezone.utc) + timedelta(minutes=8))
    monitor = Monitor(config, headless=True, sources=[FakeSource([nearly])])
    try:
        monitor.tracker.poll()
        assert monitor._next_poll_delay() <= 60.0
    finally:
        monitor.notifier.stop()


def test_poll_is_brisk_while_waiting_for_a_new_window(tmp_path, fake_claude):
    command, _ = fake_claude
    config = make_config(tmp_path, command, poll_interval=600.0)

    expired = make_snapshot(datetime.now(timezone.utc) - timedelta(seconds=1))
    monitor = Monitor(config, headless=True, sources=[FakeSource([expired])])
    try:
        monitor.tracker.poll()
        assert monitor._next_poll_delay() <= 30.0
    finally:
        monitor.notifier.stop()


def test_poll_never_sails_past_the_reset(tmp_path, fake_claude):
    """The idle delay must always leave room to observe the boundary."""
    command, _ = fake_claude
    config = make_config(tmp_path, command, poll_interval=3600.0)

    snapshot = make_snapshot(datetime.now(timezone.utc) + timedelta(minutes=20))
    monitor = Monitor(config, headless=True, sources=[FakeSource([snapshot])])
    try:
        monitor.tracker.poll()
        delay = monitor._next_poll_delay()
        assert delay < 20 * 60, "would wake after the window already reset"
    finally:
        monitor.notifier.stop()


def test_signal_forces_exit_under_a_gui(tmp_path, fake_claude, monkeypatch):
    """A GUI front-end owns the main thread, so setting a flag is not enough.

    Regression: SIGTERM logged "shutting down" but the process kept polling,
    so `pkill` left a stale instance alive and relaunching re-activated the old
    build instead of the newly installed one.
    """
    import signal as signal_mod
    import threading as threading_mod

    command, _ = fake_claude
    config = make_config(tmp_path, command)
    active = make_snapshot(datetime.now(timezone.utc) + timedelta(hours=2))
    monitor = Monitor(config, headless=True, sources=[FakeSource([active])])

    exits: list[int] = []
    monkeypatch.setattr("os._exit", lambda code: exits.append(code))

    timers: list = []
    real_timer = threading_mod.Timer

    def capture(interval, fn):
        t = real_timer(0.01, fn)
        timers.append(t)
        return t

    monkeypatch.setattr("threading.Timer", capture)

    # Pretend a GUI front-end is driving it.
    monitor._bg_thread = threading_mod.current_thread()
    handlers = {}
    monkeypatch.setattr(
        signal_mod, "signal", lambda sig, fn: handlers.setdefault(sig, fn)
    )
    monitor._install_signal_handlers()

    handler = handlers.get(signal_mod.SIGTERM)
    assert handler is not None, "SIGTERM handler was not installed"
    handler(signal_mod.SIGTERM, None)

    assert monitor._stop.is_set()
    assert timers, "no forced-exit timer was scheduled"
    for t in timers:
        t.join(2.0)
    assert exits == [0], "the process would have survived SIGTERM"
