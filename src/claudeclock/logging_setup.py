"""Structured logging with rotation, plus the append-only event ledger.

Two distinct sinks, deliberately:

* the **log** (`monitor.log`) is diagnostic chatter, rotated and disposable;
* the **ledger** (`events.jsonl`) is the durable record the user asked for -
  every reset, every message sent, every session start - one JSON object per
  line, never rotated, safe to tail or feed to `jq`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def _trace(self: logging.Logger, message: str, *args: Any, **kwargs: Any) -> None:
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


logging.Logger.trace = _trace  # type: ignore[attr-defined]


class JsonFormatter(logging.Formatter):
    """One JSON object per record, with any `extra=` fields merged in."""

    _RESERVED = frozenset(
        vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
    ) | {"message", "asctime", "taskName"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    value = repr(value)
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class ContextFormatter(logging.Formatter):
    """Human-readable text, with structured extras appended as key=value."""

    _RESERVED = JsonFormatter._RESERVED

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in self._RESERVED and not k.startswith("_")
        }
        if extras:
            rendered = " ".join(f"{k}={v!r}" for k, v in sorted(extras.items()))
            base = f"{base} | {rendered}"
        return base


def setup_logging(
    *,
    level: str,
    log_file: Path,
    json_logs: bool,
    max_bytes: int,
    backup_count: int,
    console: bool = False,
) -> logging.Logger:
    """Configure the root logger. Idempotent.

    `console=False` by default because the Rich live dashboard owns the
    terminal; writing log lines into it would corrupt the render. In headless
    (`--no-ui`) mode the caller passes `console=True`.
    """
    numeric = TRACE if level.upper() == "TRACE" else getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(numeric)
    if json_logs:
        file_handler.setFormatter(JsonFormatter())
    else:
        file_handler.setFormatter(
            ContextFormatter(
                "%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root.addHandler(file_handler)

    if console:
        # Imported lazily: only the terminal front-ends ever ask for console
        # output, and this keeps Rich out of the packaged GUI app.
        from rich.logging import RichHandler

        stream = RichHandler(
            rich_tracebacks=True, show_path=False, omit_repeated_times=False
        )
        stream.setLevel(numeric)
        # ContextFormatter, not a bare "%(message)s": in headless mode the
        # structured extras *are* the output, so dropping them would leave the
        # user watching lines that say "status" and nothing else.
        stream.setFormatter(ContextFormatter("%(message)s"))
        root.addHandler(stream)

    # These are chatty at DEBUG and tell us nothing we want. APScheduler in
    # particular logs every wakeup, which buries our own output at -v.
    for noisy in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(max(numeric, logging.WARNING))

    return logging.getLogger("cclock")


class EventLog:
    """Append-only JSONL ledger. Thread-safe, fsync-free, crash-tolerant."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._log = logging.getLogger("cclock.events")

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "pid": os.getpid(),
        }
        for key, value in fields.items():
            if isinstance(value, datetime):
                value = value.astimezone(timezone.utc).isoformat()
            entry[key] = value

        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as exc:
            # The ledger is important but never worth crashing the monitor for.
            self._log.error("could not write event ledger", extra={"error": str(exc)})

        self._log.info("event: %s", event, extra={k: v for k, v in fields.items()})
        return entry

    def tail(self, count: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()[-count:]
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def install_excepthook(logger: logging.Logger) -> None:
    """Make sure an unhandled crash lands in the log file, not just stderr."""

    def _hook(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical(
            "unhandled exception", exc_info=(exc_type, exc_value, exc_tb)
        )

    sys.excepthook = _hook
