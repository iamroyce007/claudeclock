"""Configuration loading.

Everything is read from the environment, optionally seeded from a `.env` file.
`Config.load()` is the only entry point; it never raises on a missing file, and
it validates aggressively so that a typo surfaces at startup rather than five
hours later when a threshold is supposed to fire.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_STATE_DIR = Path.home() / ".claudeclock"
VALID_SOURCES = ("statusline", "oauth", "local")


class ConfigError(ValueError):
    """Raised when the configuration is present but unusable."""


def _get(key: str, default: str) -> str:
    value = os.environ.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _get_opt(key: str) -> str | None:
    value = os.environ.get(key)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _get_bool(key: str, default: bool) -> bool:
    raw = _get(key, "true" if default else "false").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be a boolean, got {raw!r}")


def _get_float(key: str, default: float, *, minimum: float | None = None) -> float:
    raw = _get(key, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _get_int(key: str, default: int, *, minimum: int | None = None) -> int:
    raw = _get(key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    # Window model
    window_hours: float = 5.0
    poll_interval: float = 600.0
    ui_refresh: float = 1.0

    # Sources
    sources: tuple[str, ...] = VALID_SOURCES
    statusline_file: Path = DEFAULT_STATE_DIR / "statusline.json"

    # Auth
    oauth_token: str | None = None
    allow_token_refresh: bool = False

    # Trigger
    auto_trigger: bool = True
    trigger_prompt: str = "Hi"
    trigger_command: str = "claude -p {prompt} --output-format json"
    trigger_model: str | None = "claude-haiku-4-5-20251001"
    trigger_delay: float = 15.0
    trigger_timeout: float = 120.0
    trigger_max_retries: int = 5

    # GUI front-ends
    menubar_seconds: bool = True
    theme: str = "dark"

    # Notifications
    alert_thresholds: tuple[int, ...] = (30, 10, 5)
    desktop_notifications: bool = True
    webhook_url: str | None = None
    discord_webhook_url: str | None = None
    slack_webhook_url: str | None = None
    webhook_timeout: float = 10.0

    # Logging / state
    state_dir: Path = DEFAULT_STATE_DIR
    log_level: str = "INFO"
    log_json: bool = False
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5

    # Resilience
    clock_jump_threshold: float = 90.0
    backoff_min: float = 5.0
    backoff_max: float = 300.0

    # Runtime-only (not read from env)
    verbose: bool = False
    config_path: Path | None = field(default=None, compare=False)

    # -- derived ------------------------------------------------------------

    @property
    def window_seconds(self) -> float:
        return self.window_hours * 3600.0

    @property
    def log_file(self) -> Path:
        return self.state_dir / "monitor.log"

    @property
    def event_log_file(self) -> Path:
        """Append-only JSONL ledger of resets, triggers and session starts."""
        return self.state_dir / "events.jsonl"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def live_file(self) -> Path:
        """Per-second snapshot the menu bar app and detail panel read."""
        return self.state_dir / "live.json"

    def trigger_argv(self) -> list[str]:
        """Build the argv for the re-arm command.

        `{prompt}` is substituted *after* shell-splitting so that a prompt
        containing spaces stays a single argv entry and never reaches a shell.
        """
        parts = shlex.split(self.trigger_command)
        argv: list[str] = []
        for part in parts:
            if part == "{prompt}":
                argv.append(self.trigger_prompt)
            elif "{prompt}" in part:
                argv.append(part.replace("{prompt}", self.trigger_prompt))
            else:
                argv.append(part)
        # Only supply a model when the command does not already name one.
        # Testing for the bare "--model" entry missed the equally valid
        # "--model=x" spelling, so a command written that way had this
        # default appended after it - and the later flag is the one the CLI
        # honours, silently overriding the model the user asked for.
        names_model = any(
            part == "--model" or part.startswith("--model=") for part in argv
        )
        if self.trigger_model and not names_model:
            argv += ["--model", self.trigger_model]
        return argv

    # -- loading ------------------------------------------------------------

    @classmethod
    def load(cls, config_path: str | Path | None = None, *, verbose: bool = False) -> Config:
        resolved: Path | None = None
        if config_path is not None:
            resolved = Path(config_path).expanduser()
            if not resolved.exists():
                raise ConfigError(f"config file not found: {resolved}")
            load_dotenv(resolved, override=False)
        else:
            for candidate in (Path.cwd() / ".env", DEFAULT_STATE_DIR / ".env"):
                if candidate.exists():
                    load_dotenv(candidate, override=False)
                    resolved = candidate
                    break

        state_dir = Path(_get("CLAUDECLOCK_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser()

        raw_sources = _get("CLAUDECLOCK_SOURCES", ",".join(VALID_SOURCES))
        sources = tuple(s.strip().lower() for s in raw_sources.split(",") if s.strip())
        unknown = [s for s in sources if s not in VALID_SOURCES]
        if unknown:
            raise ConfigError(
                f"CLAUDECLOCK_SOURCES contains unknown source(s) {unknown}; "
                f"valid values are {list(VALID_SOURCES)}"
            )
        if not sources:
            raise ConfigError("CLAUDECLOCK_SOURCES must list at least one source")

        raw_thresholds = _get("CLAUDECLOCK_ALERT_THRESHOLDS", "30,10,5")
        try:
            thresholds = tuple(
                sorted({int(t.strip()) for t in raw_thresholds.split(",") if t.strip()},
                       reverse=True)
            )
        except ValueError as exc:
            raise ConfigError(
                f"CLAUDECLOCK_ALERT_THRESHOLDS must be comma-separated integers, got {raw_thresholds!r}"
            ) from exc
        if any(t <= 0 for t in thresholds):
            raise ConfigError("CLAUDECLOCK_ALERT_THRESHOLDS values must be positive")
        if not thresholds:
            # The tracker takes min() of this on every tick to decide when a
            # window becomes EXPIRING, and min(()) raises. A value like "," or
            # ", ," parses to an empty tuple, so reject it here rather than
            # crashing the paint loop once a second, five hours in.
            raise ConfigError(
                "CLAUDECLOCK_ALERT_THRESHOLDS must list at least one threshold"
            )

        statusline_default = state_dir / "statusline.json"
        statusline_file = Path(
            _get("CLAUDECLOCK_STATUSLINE_FILE", str(statusline_default))
        ).expanduser()

        log_level = _get("CLAUDECLOCK_LOG_LEVEL", "INFO").upper()
        if verbose and log_level not in ("DEBUG", "TRACE"):
            log_level = "DEBUG"
        if log_level not in ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"CLAUDECLOCK_LOG_LEVEL invalid: {log_level!r}")

        theme = _get("CLAUDECLOCK_THEME", "dark").lower()
        if theme not in ("dark", "light", "auto"):
            raise ConfigError(
                f"CLAUDECLOCK_THEME must be dark, light or auto; got {theme!r}"
            )

        backoff_min = _get_float("CLAUDECLOCK_BACKOFF_MIN", 5.0, minimum=0.1)
        backoff_max = _get_float("CLAUDECLOCK_BACKOFF_MAX", 300.0, minimum=0.1)
        if backoff_max < backoff_min:
            raise ConfigError("CLAUDECLOCK_BACKOFF_MAX must be >= CLAUDECLOCK_BACKOFF_MIN")

        return cls(
            window_hours=_get_float("CLAUDECLOCK_WINDOW_HOURS", 5.0, minimum=0.1),
            poll_interval=_get_float("CLAUDECLOCK_POLL_INTERVAL", 600.0, minimum=30.0),
            ui_refresh=_get_float("CLAUDECLOCK_UI_REFRESH", 1.0, minimum=0.1),
            sources=sources,
            statusline_file=statusline_file,
            oauth_token=_get_opt("CLAUDECLOCK_OAUTH_TOKEN"),
            allow_token_refresh=_get_bool("CLAUDECLOCK_ALLOW_TOKEN_REFRESH", False),
            auto_trigger=_get_bool("CLAUDECLOCK_AUTO_TRIGGER", True),
            trigger_prompt=_get("CLAUDECLOCK_TRIGGER_PROMPT", "Hi"),
            trigger_command=_get(
                "CLAUDECLOCK_TRIGGER_COMMAND", "claude -p {prompt} --output-format json"
            ),
            trigger_model=_get_opt("CLAUDECLOCK_TRIGGER_MODEL") or "claude-haiku-4-5-20251001",
            trigger_delay=_get_float("CLAUDECLOCK_TRIGGER_DELAY", 15.0, minimum=0.0),
            trigger_timeout=_get_float("CLAUDECLOCK_TRIGGER_TIMEOUT", 120.0, minimum=5.0),
            trigger_max_retries=_get_int("CLAUDECLOCK_TRIGGER_MAX_RETRIES", 5, minimum=0),
            menubar_seconds=_get_bool("CLAUDECLOCK_MENUBAR_SECONDS", True),
            theme=theme,
            alert_thresholds=thresholds,
            desktop_notifications=_get_bool("CLAUDECLOCK_DESKTOP_NOTIFICATIONS", True),
            webhook_url=_get_opt("CLAUDECLOCK_WEBHOOK_URL"),
            discord_webhook_url=_get_opt("CLAUDECLOCK_DISCORD_WEBHOOK_URL"),
            slack_webhook_url=_get_opt("CLAUDECLOCK_SLACK_WEBHOOK_URL"),
            webhook_timeout=_get_float("CLAUDECLOCK_WEBHOOK_TIMEOUT", 10.0, minimum=1.0),
            state_dir=state_dir,
            log_level=log_level,
            log_json=_get_bool("CLAUDECLOCK_LOG_JSON", False),
            log_max_bytes=_get_int("CLAUDECLOCK_LOG_MAX_BYTES", 5 * 1024 * 1024, minimum=1024),
            log_backup_count=_get_int("CLAUDECLOCK_LOG_BACKUP_COUNT", 5, minimum=0),
            clock_jump_threshold=_get_float("CLAUDECLOCK_CLOCK_JUMP_THRESHOLD", 90.0, minimum=5.0),
            backoff_min=backoff_min,
            backoff_max=backoff_max,
            verbose=verbose,
            config_path=resolved,
        )

    def ensure_state_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.statusline_file.parent.mkdir(parents=True, exist_ok=True)
