"""Secondary source: the Claude Code statusLine hook.

Claude Code pipes a documented JSON blob to the configured `statusLine` command
on every render. Since v2.x that blob carries::

    "rate_limits": {
      "five_hour":  {"used_percentage": <0-100>, "resets_at": <epoch seconds>},
      "seven_day":  {"used_percentage": <0-100>, "resets_at": <epoch seconds>}
    }

`sill install-statusline` registers a tiny shim that tees this blob to a state
file, which this source then reads. It costs no network calls and works while
offline, but it only updates while a Claude Code session is actually rendering
- hence "reported" rather than "authoritative", and hence the staleness guard.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import Confidence, Snapshot, parse_timestamp

log = logging.getLogger("sill.source.statusline")

# Beyond this, a statusline reading tells us more about when Claude Code was
# last open than about the current window.
MAX_STALENESS = timedelta(minutes=30)


class StatuslineSource:
    """Reads the snapshot file written by the statusline shim."""

    name = "statusline"

    def __init__(self, path: Path, *, max_staleness: timedelta = MAX_STALENESS) -> None:
        self.path = path
        self.max_staleness = max_staleness
        self.last_error: str | None = None
        self._warned_missing = False

    def fetch(self) -> Snapshot | None:
        if not self.path.exists():
            if not self._warned_missing:
                log.debug(
                    "statusline file absent; run `sill install-statusline` to enable "
                    "this source",
                    extra={"path": str(self.path)},
                )
                self._warned_missing = True
            self.last_error = "not installed"
            return None
        self._warned_missing = False

        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            self.last_error = f"unreadable: {exc}"
            log.debug("statusline file unreadable", extra={"error": str(exc)})
            return None

        if not raw:
            self.last_error = "empty"
            return None

        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            # A partially written file is normal if we read mid-write; the next
            # poll will get a clean one.
            self.last_error = f"malformed: {exc}"
            log.debug("statusline file malformed", extra={"error": str(exc)})
            return None

        if not isinstance(data, dict):
            self.last_error = "unexpected shape"
            return None

        written_at = parse_timestamp(data.get("_cwm_written_at"))
        now = datetime.now(timezone.utc)
        if written_at is not None and (now - written_at) > self.max_staleness:
            age = now - written_at
            self.last_error = f"stale ({int(age.total_seconds() // 60)}m old)"
            log.log(5, "statusline snapshot too old", extra={"age_s": age.total_seconds()})
            return None

        limits = data.get("rate_limits")
        if not isinstance(limits, dict):
            self.last_error = "no rate_limits (subscriber-only, after first response)"
            return None

        five = limits.get("five_hour")
        if not isinstance(five, dict):
            self.last_error = "no five_hour window reported"
            return None

        resets_at = parse_timestamp(five.get("resets_at"))
        if resets_at is None:
            self.last_error = "five_hour.resets_at missing"
            return None

        utilization = five.get("used_percentage")
        utilization = float(utilization) if isinstance(utilization, (int, float)) else None

        weekly = limits.get("seven_day")
        weekly_util = weekly_reset = None
        if isinstance(weekly, dict):
            weekly_reset = parse_timestamp(weekly.get("resets_at"))
            raw_weekly = weekly.get("used_percentage")
            if isinstance(raw_weekly, (int, float)):
                weekly_util = float(raw_weekly)

        self.last_error = None
        return Snapshot(
            resets_at=resets_at,
            utilization=utilization,
            weekly_utilization=weekly_util,
            weekly_resets_at=weekly_reset,
            source="statusline",
            confidence=Confidence.REPORTED,
            observed_at=written_at or now,
            raw={"rate_limits": limits},
        )

    def close(self) -> None:
        return None


# NOTE: substituted with a plain string replace, not str.format / %-formatting,
# so the script body can use braces and % freely without escaping games.
_TARGET_SENTINEL = "__SILL_TARGET_PATH__"

SHIM_SOURCE = '''#!/usr/bin/env python3
"""Claude Code statusLine shim installed by windowsill.

Reads the statusline JSON on stdin, tees it to a state file for the monitor,
and prints a one-line status back to Claude Code. Never fails loudly: a broken
statusline command would degrade the Claude Code UI, which is not worth it.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

TARGET = os.environ.get("SILL_STATUSLINE_FILE") or r"__SILL_TARGET_PATH__"


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0

    data["_cwm_written_at"] = datetime.now(timezone.utc).isoformat()

    try:
        parent = os.path.dirname(TARGET) or "."
        os.makedirs(parent, exist_ok=True)
        # Atomic replace so the monitor never reads a half-written file.
        fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, TARGET)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception:
        pass

    try:
        parts = []
        model = (data.get("model") or {}).get("display_name")
        if model:
            parts.append(str(model))

        five = (data.get("rate_limits") or {}).get("five_hour") or {}
        pct = five.get("used_percentage")
        if isinstance(pct, (int, float)):
            parts.append("5h {:.0f}%".format(pct))

        resets = five.get("resets_at")
        if isinstance(resets, (int, float)):
            left = int(resets - datetime.now(timezone.utc).timestamp())
            if left > 0:
                parts.append("resets in {}h{:02d}m".format(left // 3600,
                                                           (left % 3600) // 60))
        sys.stdout.write(" | ".join(parts))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def render_shim(target: Path) -> str:
    """Render the shim script that tees statusline JSON to `target`."""
    return SHIM_SOURCE.replace(_TARGET_SENTINEL, str(target))
