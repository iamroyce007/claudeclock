"""Notification fan-out: native desktop plus optional webhooks.

Every channel is best-effort and independently isolated. A failing Slack
webhook must never stop the desktop notification, and neither may ever raise
into the scheduler. Delivery happens on a small worker thread so a slow
endpoint cannot stall the countdown.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import Config

log = logging.getLogger("sill.notify")

APP_NAME = "Windowsill"


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    level: str = "info"  # info | warning | success | error
    fields: dict[str, Any] | None = None

    @property
    def colour(self) -> int:
        return {
            "info": 0x5865F2,
            "success": 0x2ECC71,
            "warning": 0xF39C12,
            "error": 0xE74C3C,
        }.get(self.level, 0x5865F2)

    @property
    def emoji(self) -> str:
        return {
            "info": "ℹ️",
            "success": "✅",
            "warning": "⏰",
            "error": "❌",
        }.get(self.level, "ℹ️")


# --------------------------------------------------------------------------
# Desktop
# --------------------------------------------------------------------------


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _notify_macos(note: Notification) -> bool:
    """Prefer terminal-notifier (survives more contexts); fall back to osascript."""
    if shutil.which("terminal-notifier"):
        try:
            subprocess.run(
                [
                    "terminal-notifier",
                    "-title", APP_NAME,
                    "-subtitle", note.title,
                    "-message", note.message,
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("terminal-notifier failed", extra={"error": str(exc)})

    script = (
        f'display notification "{_escape_applescript(note.message)}" '
        f'with title "{_escape_applescript(APP_NAME)}" '
        f'subtitle "{_escape_applescript(note.title)}"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("osascript failed", extra={"error": str(exc)})
        return False
    if result.returncode != 0:
        log.debug(
            "osascript returned non-zero",
            extra={"stderr": result.stderr.decode("utf-8", "replace")[:200]},
        )
        return False
    return True


_PS_TOAST = """
$ErrorActionPreference = 'Stop'
try {{
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $texts = $template.GetElementsByTagName('text')
    $texts.Item(0).AppendChild($template.CreateTextNode('{title}')) | Out-Null
    $texts.Item(1).AppendChild($template.CreateTextNode('{message}')) | Out-Null
    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app}').Show($toast)
}} catch {{
    Add-Type -AssemblyName System.Windows.Forms
    $icon = New-Object System.Windows.Forms.NotifyIcon
    $icon.Icon = [System.Drawing.SystemIcons]::Information
    $icon.Visible = $true
    $icon.ShowBalloonTip(10000, '{title}', '{message}', 'Info')
    Start-Sleep -Seconds 6
    $icon.Dispose()
}}
"""


def _escape_ps(text: str) -> str:
    return text.replace("'", "''")


def _notify_windows(note: Notification) -> bool:
    """Native toast via WinRT, falling back to a tray balloon on older hosts."""
    script = _PS_TOAST.format(
        title=_escape_ps(f"{APP_NAME}: {note.title}"),
        message=_escape_ps(note.message),
        app=_escape_ps(APP_NAME),
    )
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        log.debug("no powershell available for desktop notification")
        return False
    try:
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("powershell toast failed", extra={"error": str(exc)})
        return False
    return result.returncode == 0


def _notify_linux(note: Notification) -> bool:
    if not shutil.which("notify-send"):
        return False
    urgency = "critical" if note.level in ("warning", "error") else "normal"
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-a", APP_NAME,
             note.title, note.message],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def send_desktop(note: Notification) -> bool:
    if sys.platform == "darwin":
        return _notify_macos(note)
    if sys.platform.startswith("win"):
        return _notify_windows(note)
    return _notify_linux(note)


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------


def _post(url: str, payload: dict[str, Any], timeout: float, label: str) -> bool:
    try:
        response = httpx.post(url, json=payload, timeout=timeout)
        if response.status_code >= 400:
            log.warning(
                "%s webhook rejected", label,
                extra={"status": response.status_code, "body": response.text[:200]},
            )
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("%s webhook failed", label, extra={"error": str(exc)})
        return False


def _discord_payload(note: Notification) -> dict[str, Any]:
    embed: dict[str, Any] = {
        "title": f"{note.emoji} {note.title}",
        "description": note.message,
        "color": note.colour,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": APP_NAME},
    }
    if note.fields:
        embed["fields"] = [
            {"name": str(k), "value": str(v), "inline": True}
            for k, v in list(note.fields.items())[:25]
        ]
    return {"embeds": [embed]}


def _slack_payload(note: Notification) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{note.emoji} {note.title}*\n{note.message}",
            },
        }
    ]
    if note.fields:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{k}*\n{v}"}
                    for k, v in list(note.fields.items())[:10]
                ],
            }
        )
    return {"text": f"{note.title}: {note.message}", "blocks": blocks}


def _generic_payload(note: Notification) -> dict[str, Any]:
    return {
        "source": "windowsill",
        "title": note.title,
        "message": note.message,
        "level": note.level,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": note.fields or {},
    }


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------


class Notifier:
    """Queues notifications and delivers them on a background worker."""

    _SENTINEL = object()

    def __init__(self, config: Config) -> None:
        self.config = config
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=100)
        self._thread = threading.Thread(
            target=self._run, name="sill-notifier", daemon=True
        )
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._thread.start()
            self._started = True

    def send(self, note: Notification) -> None:
        if not self._started:
            self.start()
        try:
            self._queue.put_nowait(note)
        except queue.Full:
            log.warning("notification queue full, dropping", extra={"title": note.title})

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._SENTINEL:
                self._queue.task_done()
                return
            try:
                self._deliver(item)
            except Exception:
                log.exception("notification delivery crashed")
            finally:
                self._queue.task_done()

    def _deliver(self, note: Notification) -> None:
        results: dict[str, bool] = {}

        if self.config.desktop_notifications:
            results["desktop"] = send_desktop(note)

        timeout = self.config.webhook_timeout
        if self.config.discord_webhook_url:
            results["discord"] = _post(
                self.config.discord_webhook_url, _discord_payload(note), timeout, "discord"
            )
        if self.config.slack_webhook_url:
            results["slack"] = _post(
                self.config.slack_webhook_url, _slack_payload(note), timeout, "slack"
            )
        if self.config.webhook_url:
            results["webhook"] = _post(
                self.config.webhook_url, _generic_payload(note), timeout, "webhook"
            )

        log.debug(
            "notification dispatched",
            extra={"title": note.title, "level": note.level, "results": results},
        )

    def stop(self, timeout: float = 5.0) -> None:
        if not self._started:
            return
        try:
            self._queue.put_nowait(self._SENTINEL)
        except queue.Full:
            return
        self._thread.join(timeout=timeout)

    def test(self) -> dict[str, bool]:
        """Synchronously exercise every configured channel."""
        note = Notification(
            title="Test notification",
            message="If you can see this, notifications are wired up correctly.",
            level="success",
            fields={"channel": "test", "host": sys.platform},
        )
        results: dict[str, bool] = {}
        if self.config.desktop_notifications:
            results["desktop"] = send_desktop(note)
        timeout = self.config.webhook_timeout
        if self.config.discord_webhook_url:
            results["discord"] = _post(
                self.config.discord_webhook_url, _discord_payload(note), timeout, "discord"
            )
        if self.config.slack_webhook_url:
            results["slack"] = _post(
                self.config.slack_webhook_url, _slack_payload(note), timeout, "slack"
            )
        if self.config.webhook_url:
            results["webhook"] = _post(
                self.config.webhook_url, _generic_payload(note), timeout, "webhook"
            )
        return results
