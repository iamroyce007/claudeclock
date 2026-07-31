"""The live-state channel between the monitor and its GUI front-ends.

The monitor writes a small JSON document every second; the menu bar app and the
detail panel read it. That keeps exactly one process polling Anthropic while
any number of views render from it, and it means a GUI toolkit never has to
share a process (or a main thread) with the scheduler.

Writes are atomic - a reader never sees a half-written document.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("cclock.live")

# A snapshot older than this means the writer is gone, not merely slow.
DEFAULT_MAX_AGE = 10.0


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def publish(path: Path, view: Any, *, window_hours: float) -> None:
    """Atomically write the current view for front-ends to read."""
    payload = {
        "schema": 1,
        "pid": os.getpid(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": window_hours,
        "state": view.state.value if hasattr(view.state, "value") else str(view.state),
        "session_start": _iso(view.session_start),
        "resets_at": _iso(view.resets_at),
        # `is not None`, not truthiness: a zero timedelta is falsy, so a window
        # sitting at exactly 0:00 would publish null and render as "unknown"
        # instead of "expired".
        "remaining_seconds": (
            view.remaining.total_seconds() if view.remaining is not None else None
        ),
        "elapsed_seconds": (
            view.elapsed.total_seconds() if view.elapsed is not None else None
        ),
        "progress": view.progress,
        "utilization_percent": view.utilization,
        "weekly_utilization_percent": view.weekly_utilization,
        "source": view.source,
        "confidence": view.confidence,
        "stale": view.stale,
        "degraded_reason": view.degraded_reason,
        "cycles_observed": view.cycles_observed,
        "triggers_sent": view.triggers_sent,
        "last_trigger_ok": view.last_trigger_ok,
        "source_health": dict(view.source_health or {}),
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.debug("could not publish live state", extra={"error": str(exc)})


@dataclass(frozen=True)
class LiveState:
    """A reader's view of the monitor, including whether it is still alive."""

    connected: bool
    age: float | None = None
    data: dict[str, Any] | None = None
    reason: str | None = None

    # -- convenience accessors used by the GUIs -----------------------------

    def get(self, key: str, default: Any = None) -> Any:
        if not self.data:
            return default
        value = self.data.get(key, default)
        return default if value is None else value

    @property
    def state(self) -> str:
        return self.get("state", "Unknown")

    @property
    def remaining_seconds(self) -> float | None:
        """Seconds left, extrapolated to *now*.

        The writer publishes once a second, but a reader may render faster than
        that, or the writer may briefly stall. Subtracting the document's age
        keeps the clock smooth instead of stepping.
        """
        raw = self.get("remaining_seconds")
        if raw is None:
            return None
        return max(0.0, float(raw) - (self.age or 0.0))

    @property
    def progress(self) -> float:
        value = self.get("progress", 0.0)
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0


def read(path: Path, *, max_age: float = DEFAULT_MAX_AGE) -> LiveState:
    """Read the monitor's live state, or explain why it is unavailable."""
    if not path.exists():
        return LiveState(False, reason="monitor is not running")

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return LiveState(False, reason=f"unreadable live state: {exc}")

    if not isinstance(data, dict):
        return LiveState(False, reason="malformed live state")

    written = data.get("updated_at")
    age: float | None = None
    if written:
        try:
            stamp = datetime.fromisoformat(str(written).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
        except ValueError:
            age = None

    if age is not None and age > max_age:
        pid = data.get("pid")
        alive = _pid_alive(pid) if isinstance(pid, int) else False
        reason = (
            "monitor is stalled" if alive else "monitor is not running"
        )
        return LiveState(False, age=age, data=data, reason=reason)

    return LiveState(True, age=max(0.0, age or 0.0), data=data)


def _pid_alive(pid: int) -> bool:
    """Is this process still running?

    On POSIX, signal 0 is the standard harmless liveness probe. **On Windows
    it is not**: `os.kill` there calls `TerminateProcess`, so the POSIX idiom
    would kill the very process it claims to be inspecting - including our own
    monitor. Windows needs `OpenProcess` + `GetExitCodeProcess` instead.
    """
    if pid <= 0:
        return False

    if sys.platform.startswith("win"):
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            # The handle opened, so the process exists even if we cannot read
            # its exit code.
            return True
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by someone else
    except OSError:
        return False
    return True


def wait_for(path: Path, timeout: float = 20.0) -> LiveState:
    """Block until the monitor publishes, for front-ends starting alongside it."""
    deadline = time.monotonic() + timeout
    state = read(path)
    while not state.connected and time.monotonic() < deadline:
        time.sleep(0.2)
        state = read(path)
    return state


# --------------------------------------------------------------------------
# shared formatting, so every front-end renders the clock identically
# --------------------------------------------------------------------------


def format_clock(seconds: float | None, *, compact: bool = False) -> str:
    """`4:12:37`, or `4h12m` when compact (menu bars are tight on space)."""
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if compact:
        if hours:
            return f"{hours}h{minutes:02d}m"
        if minutes:
            return f"{minutes}m{secs:02d}s"
        return f"{secs}s"
    return f"{hours}:{minutes:02d}:{secs:02d}"


def format_local(iso_value: str | None, *, with_date: bool = False) -> str:
    if not iso_value:
        return "—"
    try:
        stamp = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except ValueError:
        return "—"
    local = stamp.astimezone()
    return local.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M:%S")


def status_glyph(state: str, *, stale: bool = False) -> str:
    if stale:
        return "⚠"
    return {
        "Active": "●",
        "Expiring": "◐",
        "Reset Pending": "◌",
        "Reset Complete": "✓",
    }.get(state, "○")


GLYPH_FOR_LEVEL = {
    "normal": "●",
    "warning": "◐",
    "critical": "◔",
    "expired": "◌",
    "unknown": "○",
}


def menubar_title(state: LiveState, *, show_seconds: bool = True) -> str:
    """The text shown *in* the macOS menu bar.

    Pure, so it can be tested without standing up an NSStatusItem.

    `show_seconds` gives a true per-second clock (`● 4:12:37`). The compact
    form (`● 4h12m`) is a couple of characters shorter and only changes once a
    minute above an hour, which some people prefer next to their other menu bar
    items; `CLAUDECLOCK_MENUBAR_SECONDS=false` selects it.
    """
    if not state.connected:
        return "⏳ —"
    seconds = state.remaining_seconds
    level = urgency(seconds)
    glyph = GLYPH_FOR_LEVEL.get(level, "○")
    if level == "expired":
        return f"{glyph} reset"
    return f"{glyph} {format_clock(seconds, compact=not show_seconds)}"


def tray_tooltip(state: LiveState) -> str:
    """Multi-line hover text for the Windows/Linux tray icon."""
    if not state.connected:
        return f"Claude — {state.reason or 'not connected'}"
    used = state.get("utilization_percent")
    used_text = f"{float(used):.0f}% used" if isinstance(used, (int, float)) else "—"
    return (
        f"Claude — {state.state}\n"
        f"{format_clock(state.remaining_seconds)} remaining · {used_text}\n"
        f"Resets at {format_local(state.get('resets_at'))}"
    )


def urgency(seconds: float | None) -> str:
    """`normal` | `warning` | `critical` | `expired`, for colour decisions."""
    if seconds is None:
        return "unknown"
    if seconds <= 0:
        return "expired"
    if seconds <= 5 * 60:
        return "critical"
    if seconds <= 30 * 60:
        return "warning"
    return "normal"
