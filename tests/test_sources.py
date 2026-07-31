"""Source parsing and snapshot semantics."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from claudeclock.sources import Confidence, Snapshot, parse_timestamp
from claudeclock.sources.local import detect_clock_jump
from claudeclock.sources.oauth_usage import OAuthUsageSource
from claudeclock.sources.statusline import StatuslineSource, render_shim


# --------------------------------------------------------------------------
# timestamp parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-07-31T16:40:00+00:00", datetime(2026, 7, 31, 16, 40, tzinfo=timezone.utc)),
        ("2026-07-31T16:40:00Z", datetime(2026, 7, 31, 16, 40, tzinfo=timezone.utc)),
        ("2026-07-31T16:40:00.714082+00:00",
         datetime(2026, 7, 31, 16, 40, 0, 714082, tzinfo=timezone.utc)),
        (1785516000, datetime(2026, 7, 31, 16, 40, tzinfo=timezone.utc)),
        (1785516000.0, datetime(2026, 7, 31, 16, 40, tzinfo=timezone.utc)),
        (1785516000000, datetime(2026, 7, 31, 16, 40, tzinfo=timezone.utc)),  # ms
        ("1785516000", datetime(2026, 7, 31, 16, 40, tzinfo=timezone.utc)),
    ],
)
def test_parse_timestamp_accepts_every_shape(value, expected):
    assert parse_timestamp(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-date", {}, [], True])
def test_parse_timestamp_rejects_junk(value):
    assert parse_timestamp(value) is None


def test_naive_datetime_is_assumed_utc():
    parsed = parse_timestamp(datetime(2026, 7, 31, 16, 40))
    assert parsed == datetime(2026, 7, 31, 16, 40, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# snapshot maths
# --------------------------------------------------------------------------


def test_session_start_is_reset_minus_window():
    resets_at = datetime(2026, 7, 31, 16, 40, tzinfo=timezone.utc)
    snapshot = Snapshot(resets_at=resets_at)
    assert snapshot.session_start(5.0) == datetime(2026, 7, 31, 11, 40, tzinfo=timezone.utc)


def test_window_id_is_stable_across_subsecond_jitter():
    base = datetime(2026, 7, 31, 16, 40, 0, 100_000, tzinfo=timezone.utc)
    jittered = base.replace(microsecond=900_000)
    assert Snapshot(resets_at=base).window_id() == Snapshot(resets_at=jittered).window_id()


def test_empty_window_has_no_id_or_start():
    snapshot = Snapshot(resets_at=None)
    assert snapshot.window_id() is None
    assert snapshot.session_start(5.0) is None
    assert snapshot.remaining() is None
    assert not snapshot.has_window


# --------------------------------------------------------------------------
# oauth payload parsing
# --------------------------------------------------------------------------


def test_parses_the_documented_payload():
    payload = {
        "five_hour": {
            "utilization": 6.0,
            "resets_at": "2026-07-31T16:40:00.714082+00:00",
        },
        "seven_day": None,
        "limits": [
            {
                "kind": "session",
                "percent": 6,
                "is_active": True,
                "resets_at": "2026-07-31T16:40:00.714082+00:00",
            }
        ],
    }
    snapshot = OAuthUsageSource._to_snapshot(payload)

    assert snapshot.resets_at == datetime(2026, 7, 31, 16, 40, 0, 714082, tzinfo=timezone.utc)
    assert snapshot.utilization == 6.0
    assert snapshot.confidence == Confidence.AUTHORITATIVE
    assert snapshot.source == "oauth"


def test_null_five_hour_means_no_open_window():
    snapshot = OAuthUsageSource._to_snapshot({"five_hour": None, "seven_day": None})
    assert snapshot.resets_at is None
    assert not snapshot.has_window


def test_falls_back_to_the_limits_array():
    """`limits[]` carries the same data and is the newer shape."""
    payload = {
        "five_hour": None,
        "limits": [
            {"kind": "weekly", "is_active": True, "resets_at": "2026-08-05T00:00:00Z"},
            {
                "kind": "session",
                "percent": 42,
                "is_active": True,
                "resets_at": "2026-07-31T16:40:00Z",
            },
        ],
    }
    snapshot = OAuthUsageSource._to_snapshot(payload)

    assert snapshot.resets_at == datetime(2026, 7, 31, 16, 40, tzinfo=timezone.utc)
    assert snapshot.utilization == 42.0


def test_inactive_session_limit_is_ignored():
    payload = {
        "five_hour": None,
        "limits": [
            {"kind": "session", "is_active": False, "resets_at": "2026-07-31T16:40:00Z"}
        ],
    }
    assert OAuthUsageSource._to_snapshot(payload).resets_at is None


def test_weekly_limit_is_carried_through():
    payload = {
        "five_hour": {"utilization": 1.0, "resets_at": "2026-07-31T16:40:00Z"},
        "seven_day": {"utilization": 55.5, "resets_at": "2026-08-05T00:00:00Z"},
    }
    snapshot = OAuthUsageSource._to_snapshot(payload)
    assert snapshot.weekly_utilization == 55.5
    assert snapshot.weekly_resets_at == datetime(2026, 8, 5, tzinfo=timezone.utc)


def test_garbage_payload_does_not_raise():
    for payload in ({}, {"five_hour": "nonsense"}, {"limits": "nope"},
                    {"five_hour": {"resets_at": None}}):
        assert OAuthUsageSource._to_snapshot(payload).resets_at is None


# --------------------------------------------------------------------------
# statusline source
# --------------------------------------------------------------------------


def _write_statusline(path, *, resets_in_seconds=3600, written_at=None, **overrides):
    now = datetime.now(timezone.utc)
    payload = {
        "rate_limits": {
            "five_hour": {
                "used_percentage": 12.5,
                "resets_at": (now + timedelta(seconds=resets_in_seconds)).timestamp(),
            }
        },
        "_cwm_written_at": (written_at or now).isoformat(),
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_statusline_reads_a_fresh_snapshot(tmp_path):
    path = tmp_path / "statusline.json"
    _write_statusline(path)
    snapshot = StatuslineSource(path).fetch()

    assert snapshot is not None
    assert snapshot.utilization == 12.5
    assert snapshot.confidence == Confidence.REPORTED


def test_statusline_rejects_a_stale_snapshot(tmp_path):
    """A file that stopped updating tells us about Claude Code, not the window."""
    path = tmp_path / "statusline.json"
    _write_statusline(path, written_at=datetime.now(timezone.utc) - timedelta(hours=3))
    source = StatuslineSource(path)

    assert source.fetch() is None
    assert "stale" in source.last_error


def test_statusline_survives_a_partial_write(tmp_path):
    path = tmp_path / "statusline.json"
    path.write_text('{"rate_limits": {"five_ho', encoding="utf-8")
    source = StatuslineSource(path)

    assert source.fetch() is None
    assert "malformed" in source.last_error


def test_statusline_missing_file_is_not_an_error(tmp_path):
    source = StatuslineSource(tmp_path / "absent.json")
    assert source.fetch() is None
    assert source.last_error == "not installed"


def test_statusline_without_rate_limits(tmp_path):
    """`rate_limits` is subscriber-only and absent before the first response."""
    path = tmp_path / "statusline.json"
    path.write_text(
        json.dumps({"model": {"display_name": "Opus"},
                    "_cwm_written_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    source = StatuslineSource(path)
    assert source.fetch() is None
    assert "rate_limits" in source.last_error


def test_generated_shim_is_valid_python(tmp_path):
    target = tmp_path / "out.json"
    code = render_shim(target)
    compile(code, "shim.py", "exec")
    assert str(target) in code
    assert "__CLAUDECLOCK_TARGET_PATH__" not in code, "sentinel was not substituted"


# --------------------------------------------------------------------------
# clock jump detection
# --------------------------------------------------------------------------


def test_no_jump_when_clocks_agree():
    import time

    assert detect_clock_jump(time.time(), time.monotonic(), threshold=90.0) is None


def test_sleep_shows_up_as_a_jump():
    import time

    # Wall clock moved an hour further than monotonic: a one-hour suspend.
    drift = detect_clock_jump(time.time() - 3600, time.monotonic(), threshold=90.0)
    assert drift is not None and drift > 3500


def test_backwards_clock_change_is_also_a_jump():
    import time

    drift = detect_clock_jump(time.time() + 3600, time.monotonic(), threshold=90.0)
    assert drift is not None and drift < -3500
