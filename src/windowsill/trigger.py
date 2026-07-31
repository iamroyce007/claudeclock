"""Re-arming the next window.

A 5-hour window does not begin on a clock boundary - it begins with your first
request after the previous one lapsed. So to *start* the next window (and make
its true server-side reset time observable), something has to send a request.

We do that by shelling out to Claude Code in its documented non-interactive
mode (`claude -p "Hi" --output-format json`) on the cheapest model available.
That is an official, supported CLI entry point. No browser automation, no
private endpoints, no synthetic UI events.

The cost is one tiny prompt per 5-hour cycle, billed against the window it
opens - which is the point.
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config

log = logging.getLogger("sill.trigger")


@dataclass(frozen=True)
class TriggerResult:
    ok: bool
    attempts: int
    duration: float
    detail: str
    stdout: str = ""
    stderr: str = ""
    session_id: str | None = None


class TriggerError(RuntimeError):
    pass


def _resolve_executable(argv: list[str]) -> list[str]:
    """Resolve argv[0] on PATH so we never need shell=True.

    On Windows a bare `claude` may be a `.cmd`/`.ps1` shim, which
    `subprocess` will not find without the extension; `shutil.which` handles
    PATHEXT for us.
    """
    if not argv:
        raise TriggerError("trigger command is empty")
    resolved = shutil.which(argv[0])
    if resolved is None:
        raise TriggerError(
            f"could not find {argv[0]!r} on PATH. Set SILL_TRIGGER_COMMAND to an "
            "absolute path, or make sure Claude Code is installed."
        )
    return [resolved, *argv[1:]]


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    # Keep the child's own output machine-parseable and quiet.
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "0")
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


def send_trigger(config: Config) -> TriggerResult:
    """Send the configured prompt, retrying with exponential backoff.

    Returns rather than raises: the caller is a scheduler job, and a failed
    re-arm is a state to report, not an exception to unwind.
    """
    argv = _resolve_executable(config.trigger_argv())
    started = time.monotonic()
    attempts = 0
    last_detail = "not attempted"
    last_stdout = last_stderr = ""

    max_attempts = config.trigger_max_retries + 1
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        log.info(
            "sending re-arm prompt",
            extra={
                "attempt": attempt,
                "of": max_attempts,
                "prompt": config.trigger_prompt,
                "model": config.trigger_model,
            },
        )
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=config.trigger_timeout,
                check=False,
                env=_child_env(),
                stdin=subprocess.DEVNULL,
                cwd=str(config.state_dir),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform.startswith("win")
                else 0,
            )
        except subprocess.TimeoutExpired:
            last_detail = f"timed out after {config.trigger_timeout:.0f}s"
            log.warning("re-arm prompt timed out", extra={"attempt": attempt})
        except OSError as exc:
            last_detail = f"could not launch: {exc}"
            log.warning("re-arm launch failed", extra={"error": str(exc)})
        else:
            last_stdout = (completed.stdout or "").strip()
            last_stderr = (completed.stderr or "").strip()

            if completed.returncode == 0:
                session_id = _extract_session_id(last_stdout)
                duration = time.monotonic() - started
                log.info(
                    "re-arm prompt succeeded",
                    extra={
                        "attempt": attempt,
                        "duration": round(duration, 2),
                        "session_id": session_id,
                    },
                )
                return TriggerResult(
                    ok=True,
                    attempts=attempts,
                    duration=duration,
                    detail="ok",
                    stdout=last_stdout[:2000],
                    stderr=last_stderr[:2000],
                    session_id=session_id,
                )

            last_detail = f"exit {completed.returncode}: {(last_stderr or last_stdout)[:300]}"
            log.warning(
                "re-arm prompt failed",
                extra={"attempt": attempt, "returncode": completed.returncode,
                       "stderr": last_stderr[:300]},
            )

            if _is_usage_limited(last_stdout, last_stderr):
                # The window has not actually reset yet. Retrying in a tight
                # loop would only burn requests; back off hard and let the
                # next poll re-evaluate.
                return TriggerResult(
                    ok=False,
                    attempts=attempts,
                    duration=time.monotonic() - started,
                    detail="usage limit still in effect; will retry on next cycle",
                    stdout=last_stdout[:2000],
                    stderr=last_stderr[:2000],
                )

        if attempt < max_attempts:
            delay = min(config.backoff_max, config.backoff_min * (2 ** (attempt - 1)))
            delay *= 0.75 + random.random() * 0.5
            log.info("retrying re-arm", extra={"in_seconds": round(delay, 1)})
            time.sleep(delay)

    return TriggerResult(
        ok=False,
        attempts=attempts,
        duration=time.monotonic() - started,
        detail=last_detail,
        stdout=last_stdout[:2000],
        stderr=last_stderr[:2000],
    )


def _extract_session_id(stdout: str) -> str | None:
    """Pull the session id out of `--output-format json` output."""
    if not stdout:
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # Stream-json emits one object per line; the last complete one wins.
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            break
        else:
            return None
    if isinstance(data, dict):
        value = data.get("session_id") or data.get("sessionId")
        return str(value) if value else None
    return None


def _is_usage_limited(stdout: str, stderr: str) -> bool:
    blob = f"{stdout}\n{stderr}".lower()
    return any(
        marker in blob
        for marker in (
            "usage limit reached",
            "rate limit",
            "429",
            "limit will reset",
        )
    )


def dry_run_description(config: Config) -> str:
    """What `send_trigger` would execute, for `sill check` and the UI."""
    argv = config.trigger_argv()
    resolved = shutil.which(argv[0])
    location = resolved or f"{argv[0]} (NOT FOUND on PATH)"
    return f"{location} " + " ".join(
        f'"{a}"' if " " in a else a for a in argv[1:]
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
