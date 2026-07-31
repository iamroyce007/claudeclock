"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import Config, ConfigError
from .logging_setup import EventLog, install_excepthook, setup_logging

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cclock",
        description=(
            "Monitor the Claude 5-hour usage window in real time, and "
            "automatically open the next one when it resets."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  cclock tray                    menu bar (macOS) / taskbar (Windows) app\n"
            "  cclock panel                   just the detail window\n"
            "  cclock run                     live dashboard, auto re-arm enabled\n"
            "  cclock run --no-ui             headless, for background/service use\n"
            "  cclock status                  one-shot snapshot, then exit\n"
            "  cclock status --json           machine-readable snapshot\n"
            "  cclock check                   verify auth, sources and trigger wiring\n"
            "  cclock install-statusline      enable the offline statusline source\n"
            "  cclock log --tail 30           recent resets, triggers, session starts\n"
            "  cclock test-notify             fire a test notification everywhere\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"cclock {__version__}")
    parser.add_argument(
        "-c", "--config", metavar="PATH",
        help="path to a .env file (default: ./.env, then ~/.claudeclock/.env)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="enable debug logging (equivalent to CLAUDECLOCK_LOG_LEVEL=DEBUG)",
    )
    parser.add_argument(
        "--trace", action="store_true",
        help="maximum verbosity, including per-poll source detail",
    )

    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="start the live monitor (default)")
    run.add_argument("--no-ui", action="store_true",
                     help="headless mode: log lines instead of a dashboard")
    run.add_argument("--no-trigger", action="store_true",
                     help="never send the re-arm prompt; observe only")
    run.add_argument("--once", action="store_true",
                     help="poll once, print the result, and exit")

    status = sub.add_parser("status", help="print the current window state and exit")
    status.add_argument("--json", action="store_true", dest="as_json",
                        help="emit JSON instead of a rendered panel")

    sub.add_parser("check", help="diagnose auth, sources, and the trigger command")

    log_cmd = sub.add_parser("log", help="show the event ledger")
    log_cmd.add_argument("--tail", type=int, default=20, metavar="N",
                         help="number of events to show (default 20)")
    log_cmd.add_argument("--json", action="store_true", dest="as_json",
                         help="emit raw JSONL")

    install = sub.add_parser(
        "install-statusline",
        help="register the Claude Code statusLine hook used by the offline source",
    )
    install.add_argument("--force", action="store_true",
                         help="overwrite an existing statusLine setting")
    install.add_argument("--print-only", action="store_true",
                         help="show what would be written, change nothing")

    tray = sub.add_parser(
        "tray",
        help="run in the macOS menu bar / Windows taskbar with a live countdown",
    )
    tray.add_argument("--no-trigger", action="store_true",
                      help="never send the re-arm prompt; observe only")

    sub.add_parser(
        "panel",
        help="open just the detail window and attach to a running monitor",
    )

    sub.add_parser("test-notify", help="send a test notification to every channel")

    trig = sub.add_parser("trigger", help="send the re-arm prompt right now")
    trig.add_argument("--dry-run", action="store_true",
                      help="print the command that would run, then exit")

    return parser


def _load(args: argparse.Namespace) -> Config:
    if args.trace:
        os.environ["CLAUDECLOCK_LOG_LEVEL"] = "TRACE"
    config = Config.load(args.config, verbose=args.verbose or args.trace)
    config.ensure_state_dir()
    return config


def _init_logging(config: Config, *, console_output: bool) -> logging.Logger:
    logger = setup_logging(
        level=config.log_level,
        log_file=config.log_file,
        json_logs=config.log_json,
        max_bytes=config.log_max_bytes,
        backup_count=config.log_backup_count,
        console=console_output,
    )
    install_excepthook(logger)
    return logger


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    from .monitor import Monitor

    if args.no_trigger:
        config = Config(**{**config.__dict__, "auto_trigger": False})

    _init_logging(config, console_output=bool(args.no_ui))

    if args.once:
        return cmd_status(config, argparse.Namespace(as_json=False))

    monitor = Monitor(config, headless=bool(args.no_ui))
    return monitor.run()


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    from .tracker import WindowTracker, build_sources
    from .ui import render_once

    _init_logging(config, console_output=False)
    tracker = WindowTracker(config, build_sources(config), EventLog(config.event_log_file))
    try:
        view = tracker.poll()
    finally:
        tracker.close()

    if getattr(args, "as_json", False):
        payload = {
            "state": view.state.value,
            "session_start": view.session_start.isoformat() if view.session_start else None,
            "resets_at": view.resets_at.isoformat() if view.resets_at else None,
            "remaining_seconds": (
                view.remaining.total_seconds() if view.remaining is not None else None
            ),
            "elapsed_seconds": (
                view.elapsed.total_seconds() if view.elapsed is not None else None
            ),
            "utilization_percent": view.utilization,
            "weekly_utilization_percent": view.weekly_utilization,
            "source": view.source,
            "confidence": view.confidence,
            "window_id": view.window_id,
            "stale": view.stale,
            "degraded_reason": view.degraded_reason,
            "cycles_observed": view.cycles_observed,
            "triggers_sent": view.triggers_sent,
            "source_health": view.source_health,
        }
        print(json.dumps(payload, indent=2))
    else:
        render_once(config, view, console)
    return 0


def cmd_check(config: Config, args: argparse.Namespace) -> int:
    from .auth import AuthError, load_credentials
    from .sources.oauth_usage import OAuthUsageSource
    from .tracker import build_sources
    from .trigger import dry_run_description

    _init_logging(config, console_output=False)

    table = Table(title="claudeclock diagnostics", title_style="bold",
                  show_lines=False, header_style="bold grey70")
    table.add_column("Check", style="grey70", no_wrap=True)
    table.add_column("Result")

    ok = Text("OK", style="bold green")
    warn = Text("WARN", style="bold yellow")
    fail = Text("FAIL", style="bold red")

    table.add_row("Config file", Text(str(config.config_path or "none (using defaults)")))
    table.add_row("State dir", Text(str(config.state_dir)))
    table.add_row("Sources", Text(", ".join(config.sources)))

    # -- credentials
    try:
        creds = load_credentials(config.oauth_token)
        detail = Text()
        detail.append_text(ok)
        detail.append(f"  {creds.source}", style="grey70")
        if creds.subscription_type:
            detail.append(f", plan={creds.subscription_type}", style="grey70")
        if creds.expires_in is not None:
            mins = creds.expires_in / 60
            detail.append(f", expires in {mins:.0f}m",
                          style="grey70" if mins > 10 else "yellow")
        table.add_row("Credentials", detail)
    except AuthError as exc:
        detail = Text()
        detail.append_text(fail)
        detail.append(f"  {exc}", style="red")
        table.add_row("Credentials", detail)
        creds = None

    # -- live endpoint
    if creds is not None and "oauth" in config.sources:
        source = OAuthUsageSource(
            explicit_token=config.oauth_token,
            allow_refresh=config.allow_token_refresh,
        )
        try:
            snapshot = source.fetch()
        finally:
            source.close()
        detail = Text()
        if snapshot is None:
            detail.append_text(fail)
            detail.append(f"  {source.last_error}", style="red")
        elif snapshot.has_window:
            detail.append_text(ok)
            detail.append(
                f"  window resets {snapshot.resets_at.astimezone():%Y-%m-%d %H:%M:%S %Z}",
                style="grey70",
            )
            if snapshot.utilization is not None:
                detail.append(f", {snapshot.utilization:.1f}% used", style="grey70")
        else:
            detail.append_text(warn)
            detail.append("  reachable, but no window is currently open", style="yellow")
        table.add_row("Usage endpoint", detail)

    # -- statusline
    if "statusline" in config.sources:
        detail = Text()
        if config.statusline_file.exists():
            detail.append_text(ok)
            detail.append(f"  {config.statusline_file}", style="grey70")
        else:
            detail.append_text(warn)
            detail.append(
                "  not installed — run `cclock install-statusline`", style="yellow"
            )
        table.add_row("Statusline source", detail)

    # -- trigger
    detail = Text()
    if not config.auto_trigger:
        detail.append_text(warn)
        detail.append("  auto-trigger disabled", style="yellow")
    else:
        description = dry_run_description(config)
        if "NOT FOUND" in description:
            detail.append_text(fail)
            detail.append(f"  {description}", style="red")
        else:
            detail.append_text(ok)
            detail.append(f"  {description}", style="grey70")
    table.add_row("Re-arm command", detail)

    # -- notifications
    channels = []
    if config.desktop_notifications:
        channels.append("desktop")
    if config.discord_webhook_url:
        channels.append("discord")
    if config.slack_webhook_url:
        channels.append("slack")
    if config.webhook_url:
        channels.append("webhook")
    detail = Text()
    detail.append_text(ok if channels else warn)
    detail.append(f"  {', '.join(channels) if channels else 'none configured'}",
                  style="grey70")
    table.add_row("Notifications", detail)

    table.add_row(
        "Alert thresholds",
        Text(", ".join(f"{t}m" for t in config.alert_thresholds), style="grey70"),
    )

    console.print(table)
    # Close any sources we built for validation side effects.
    for source in build_sources(config):
        source.close()
    return 0


def cmd_log(config: Config, args: argparse.Namespace) -> int:
    ledger = EventLog(config.event_log_file)
    entries = ledger.tail(args.tail)
    if not entries:
        console.print(
            f"[grey62]no events recorded yet at {config.event_log_file}[/grey62]"
        )
        return 0

    if args.as_json:
        for entry in entries:
            print(json.dumps(entry))
        return 0

    table = Table(title=f"last {len(entries)} events", header_style="bold grey70")
    table.add_column("When", style="grey70", no_wrap=True)
    table.add_column("Event", no_wrap=True)
    table.add_column("Detail", overflow="fold")

    styles = {
        "window_reset": "magenta",
        "session_start": "cyan",
        "trigger_result": "green",
        "trigger_attempt": "yellow",
        "threshold_alert": "yellow",
        "system_resume": "red",
        "monitor_start": "grey62",
        "monitor_stop": "grey62",
    }

    for entry in entries:
        when = entry.get("ts", "")[:19].replace("T", " ")
        event = entry.get("event", "?")
        detail = {
            k: v for k, v in entry.items() if k not in ("ts", "event", "pid")
        }
        table.add_row(
            when,
            Text(event, style=styles.get(event, "white")),
            ", ".join(f"{k}={v}" for k, v in detail.items()) or "—",
        )

    console.print(table)
    return 0


def cmd_install_statusline(config: Config, args: argparse.Namespace) -> int:
    from .sources.statusline import render_shim

    settings_path = Path.home() / ".claude" / "settings.json"
    shim_path = config.state_dir / "statusline_shim.py"
    script = render_shim(config.statusline_file)

    command = f'"{sys.executable}" "{shim_path}"'

    if args.print_only:
        console.print(f"[bold]would write shim to[/bold] {shim_path}")
        console.print(f"[bold]would set statusLine.command to[/bold] {command}")
        console.print(f"[bold]in[/bold] {settings_path}")
        return 0

    shim_path.parent.mkdir(parents=True, exist_ok=True)
    shim_path.write_text(script, encoding="utf-8")
    try:
        shim_path.chmod(0o755)
    except OSError:
        pass
    console.print(f"[green]✓[/green] wrote shim: {shim_path}")

    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            console.print(
                f"[red]✗[/red] {settings_path} is not valid JSON; not touching it."
            )
            return 1

    existing = settings.get("statusLine")
    if existing and not args.force:
        console.print(
            f"[yellow]![/yellow] a statusLine is already configured:\n"
            f"    {json.dumps(existing)}\n"
            f"Re-run with --force to replace it, or chain the shim yourself:\n"
            f"    {command}"
        )
        return 1

    if settings_path.exists():
        backup = settings_path.with_suffix(".json.claudeclock-backup")
        backup.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        console.print(f"[grey62]backed up existing settings to {backup}[/grey62]")

    settings["statusLine"] = {"type": "command", "command": command, "padding": 0}
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    console.print(f"[green]✓[/green] registered statusLine in {settings_path}")
    console.print(
        "[grey62]Start a Claude Code session; the statusline source will populate "
        f"{config.statusline_file}[/grey62]"
    )
    return 0


def cmd_tray(config: Config, args: argparse.Namespace) -> int:
    from .gui.app import UnsupportedFrontend, run as run_tray

    if args.no_trigger:
        config = Config(**{**config.__dict__, "auto_trigger": False})

    _init_logging(config, console_output=False)
    try:
        return run_tray(config)
    except UnsupportedFrontend as exc:
        console.print(f"[bold red]cannot start the tray app:[/bold red] {exc}")
        return 2


def cmd_panel(config: Config, args: argparse.Namespace) -> int:
    from .gui.app import run_panel

    _init_logging(config, console_output=False)
    return run_panel(config)


def cmd_test_notify(config: Config, args: argparse.Namespace) -> int:
    from .notify import Notifier

    _init_logging(config, console_output=True)
    results = Notifier(config).test()
    if not results:
        console.print("[yellow]no notification channels are configured[/yellow]")
        return 1
    for channel, ok in results.items():
        mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
        console.print(f"{mark} {channel}")
    return 0 if all(results.values()) else 1


def cmd_trigger(config: Config, args: argparse.Namespace) -> int:
    from .trigger import dry_run_description, send_trigger

    _init_logging(config, console_output=True)

    if args.dry_run:
        console.print(dry_run_description(config))
        return 0

    ledger = EventLog(config.event_log_file)
    ledger.record("trigger_attempt", prompt=config.trigger_prompt, manual=True)
    result = send_trigger(config)
    ledger.record(
        "trigger_result",
        ok=result.ok,
        attempts=result.attempts,
        duration_seconds=round(result.duration, 2),
        detail=result.detail,
        session_id=result.session_id,
        manual=True,
    )

    if result.ok:
        console.print(
            f"[green]✓[/green] sent {config.trigger_prompt!r} "
            f"in {result.duration:.1f}s (attempt {result.attempts})"
        )
        return 0
    console.print(f"[red]✗[/red] {result.detail}")
    return 1


COMMANDS = {
    "run": cmd_run,
    "status": cmd_status,
    "check": cmd_check,
    "log": cmd_log,
    "install-statusline": cmd_install_statusline,
    "tray": cmd_tray,
    "panel": cmd_panel,
    "test-notify": cmd_test_notify,
    "trigger": cmd_trigger,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        args.command = "run"
        args.no_ui = False
        args.no_trigger = False
        args.once = False

    try:
        config = _load(args)
    except ConfigError as exc:
        console.print(f"[bold red]configuration error:[/bold red] {exc}")
        return 2

    handler = COMMANDS[args.command]
    try:
        return handler(config, args)
    except KeyboardInterrupt:
        console.print("\n[grey62]interrupted[/grey62]")
        return 130
    except Exception as exc:  # noqa: BLE001 - top level guard
        logging.getLogger("cclock").exception("command failed")
        console.print(f"[bold red]error:[/bold red] {exc}")
        if args.verbose or args.trace:
            raise
        console.print(f"[grey62]see {config.log_file} for the traceback[/grey62]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
