"""Resolve the OAuth credentials Claude Code already holds.

We are a *reader* of Claude Code's credential store, not an owner of it. The
store lives in:

* macOS  - the login Keychain, generic password ``Claude Code-credentials``
* Windows/Linux - ``~/.claude/.credentials.json``

Both hold the same JSON shape::

    {"claudeAiOauth": {"accessToken": "...", "refreshToken": "...",
                       "expiresAt": <epoch-ms>, "scopes": [...],
                       "subscriptionType": "pro"}}

Credentials are re-read on every use rather than cached, because Claude Code
rotates the access token underneath us and we want to pick that up for free.
Token *refresh* is opt-in (`CLAUDECLOCK_ALLOW_TOKEN_REFRESH`) and off by default: two
processes writing the same credential store is a race worth avoiding, and the
auto-reset shell-out runs `claude` every ~5h, which refreshes them anyway.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("cclock.auth")

KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
# Public client id for the Claude Code CLI's OAuth app.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

# Treat a token as expired slightly early so we never send one that dies
# in flight.
EXPIRY_SKEW_SECONDS = 60


class AuthError(RuntimeError):
    """No usable credentials could be found."""


@dataclass(frozen=True)
class Credentials:
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None  # epoch seconds
    subscription_type: str | None = None
    source: str = "unknown"

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - EXPIRY_SKEW_SECONDS)

    @property
    def expires_in(self) -> float | None:
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - time.time())

    def redacted(self) -> str:
        return f"{self.access_token[:14]}...({len(self.access_token)} chars)"


def _parse_blob(blob: str, source: str) -> Credentials | None:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        log.warning("credential store is not valid JSON", extra={"source": source})
        return None

    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        oauth = data if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None

    token = oauth.get("accessToken") or oauth.get("access_token")
    if not token:
        return None

    expires_at = oauth.get("expiresAt") or oauth.get("expires_at")
    if expires_at is not None:
        try:
            expires_at = float(expires_at)
            # Stored in milliseconds; anything that large is clearly not seconds.
            if expires_at > 1e11:
                expires_at /= 1000.0
        except (TypeError, ValueError):
            expires_at = None

    return Credentials(
        access_token=str(token),
        refresh_token=oauth.get("refreshToken") or oauth.get("refresh_token"),
        expires_at=expires_at,
        subscription_type=oauth.get("subscriptionType") or oauth.get("subscription_type"),
        source=source,
    )


def _read_keychain() -> Credentials | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("keychain read failed", extra={"error": str(exc)})
        return None
    if result.returncode != 0:
        log.debug("keychain entry not found", extra={"stderr": result.stderr.strip()[:200]})
        return None
    return _parse_blob(result.stdout.strip(), "keychain")


def _read_file() -> Credentials | None:
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        blob = CREDENTIALS_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("credentials file unreadable", extra={"error": str(exc)})
        return None
    return _parse_blob(blob, "file")


def _write_file(creds: Credentials) -> bool:
    """Persist refreshed credentials back to the JSON store (never Keychain)."""
    try:
        existing: dict = {}
        if CREDENTIALS_FILE.exists():
            existing = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
        oauth = existing.setdefault("claudeAiOauth", {})
        oauth["accessToken"] = creds.access_token
        if creds.refresh_token:
            oauth["refreshToken"] = creds.refresh_token
        if creds.expires_at:
            oauth["expiresAt"] = int(creds.expires_at * 1000)
        CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CREDENTIALS_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        CREDENTIALS_FILE.chmod(0o600)
        return True
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not persist refreshed token", extra={"error": str(exc)})
        return False


def load_credentials(explicit_token: str | None = None) -> Credentials:
    """Find credentials, preferring an explicit override.

    Raises `AuthError` with actionable guidance when nothing is available.
    """
    if explicit_token:
        return Credentials(access_token=explicit_token, source="config")

    for reader in (_read_keychain, _read_file):
        creds = reader()
        if creds is not None:
            log.debug(
                "loaded credentials",
                extra={
                    "source": creds.source,
                    "plan": creds.subscription_type,
                    "expires_in": creds.expires_in,
                },
            )
            return creds

    raise AuthError(
        "No Claude Code OAuth credentials found. Run `claude` once and sign in, "
        "or set CLAUDECLOCK_OAUTH_TOKEN in your .env."
    )


def refresh_credentials(creds: Credentials, *, timeout: float = 20.0) -> Credentials:
    """Exchange the refresh token for a new access token.

    Only called when `CLAUDECLOCK_ALLOW_TOKEN_REFRESH=true`. On macOS the result is held
    in memory only: we deliberately do not write to the Keychain entry that
    Claude Code owns.
    """
    if not creds.refresh_token:
        raise AuthError("stored credentials have no refresh token")

    log.info("refreshing expired access token")
    try:
        response = httpx.post(
            TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh_token,
                "client_id": CLIENT_ID,
            },
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise AuthError(f"token refresh failed: {exc}") from exc

    token = payload.get("access_token")
    if not token:
        raise AuthError("token refresh response contained no access_token")

    expires_in = payload.get("expires_in")
    refreshed = Credentials(
        access_token=token,
        refresh_token=payload.get("refresh_token") or creds.refresh_token,
        expires_at=time.time() + float(expires_in) if expires_in else None,
        subscription_type=creds.subscription_type,
        source=creds.source,
    )

    if creds.source == "file":
        _write_file(refreshed)

    return refreshed
