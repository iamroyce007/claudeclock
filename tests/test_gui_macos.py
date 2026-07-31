"""macOS menu bar behaviour that can be verified without clicking anything.

The interesting case is what happens *while the menu is open*. AppKit switches
the run loop into `NSEventTrackingRunLoopMode` for the duration, which is
exactly the condition that used to freeze the dropdown. That mode can be
entered programmatically, so the fix is directly testable.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="menu bar front-end is macOS-only"
)

pytest.importorskip("rumps", reason="needs rumps / PyObjC")

from AppKit import NSEventTrackingRunLoopMode  # noqa: E402
from CoreFoundation import CFRunLoopAddCommonMode, CFRunLoopGetCurrent  # noqa: E402
from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop  # noqa: E402

from windowsill.gui.macos import LiveTimer  # noqa: E402

INTERVAL = 0.2
DURATION = 1.5
EXPECTED = int(DURATION / INTERVAL)


def pump(mode, seconds: float) -> None:
    """Run the run loop in `mode` for `seconds`, as AppKit would."""
    end = NSDate.dateWithTimeIntervalSinceNow_(seconds)
    while NSDate.date().compare_(end) < 0:
        NSRunLoop.currentRunLoop().runMode_beforeDate_(
            mode, NSDate.dateWithTimeIntervalSinceNow_(0.05)
        )


def test_timer_fires_while_a_menu_is_open():
    """Regression: the dropdown froze the instant it was clicked open."""
    hits: list[int] = []
    timer = LiveTimer(lambda: hits.append(1), interval=INTERVAL)
    timer.start()
    try:
        pump(NSEventTrackingRunLoopMode, DURATION)
    finally:
        timer.stop()

    assert hits, "timer never fired during menu tracking — the dropdown is frozen"
    assert len(hits) >= EXPECTED - 2, f"only {len(hits)} fires, expected ~{EXPECTED}"


def test_timer_still_fires_normally():
    """The tracking-mode fix must not break the ordinary case."""
    hits: list[int] = []
    timer = LiveTimer(lambda: hits.append(1), interval=INTERVAL)
    timer.start()
    try:
        pump(NSDefaultRunLoopMode, DURATION)
    finally:
        timer.stop()

    assert len(hits) >= EXPECTED - 2


def test_timer_does_not_double_fire():
    """It is registered in two modes; CFRunLoop must de-duplicate."""
    # Put tracking into the common set, as a running NSApplication does - the
    # arrangement most likely to cause a double registration.
    CFRunLoopAddCommonMode(CFRunLoopGetCurrent(), NSEventTrackingRunLoopMode)

    hits: list[int] = []
    timer = LiveTimer(lambda: hits.append(1), interval=INTERVAL)
    timer.start()
    try:
        pump(NSEventTrackingRunLoopMode, DURATION)
    finally:
        timer.stop()

    assert len(hits) <= EXPECTED + 2, f"{len(hits)} fires — timer is double-firing"


def test_rumps_timer_would_have_frozen():
    """Documents *why* LiveTimer exists, and guards against reverting to it."""
    import rumps

    hits: list[int] = []
    timer = rumps.Timer(lambda _sender: hits.append(1), INTERVAL)
    timer.start()
    try:
        pump(NSEventTrackingRunLoopMode, DURATION)
    finally:
        timer.stop()

    assert hits == [], (
        "rumps.Timer now fires during menu tracking; if upstream fixed this, "
        "LiveTimer can be simplified"
    )


def test_stop_is_idempotent():
    timer = LiveTimer(lambda: None, interval=INTERVAL)
    timer.start()
    timer.stop()
    timer.stop()  # must not raise


def test_a_raising_callback_does_not_kill_the_tick():
    """One bad render must not stop the clock for good."""
    calls: list[int] = []

    def callback():
        calls.append(1)
        raise RuntimeError("boom")

    timer = LiveTimer(callback, interval=INTERVAL)
    timer.start()
    try:
        pump(NSDefaultRunLoopMode, DURATION)
    finally:
        timer.stop()

    assert len(calls) >= 3, "timer stopped after the first exception"
