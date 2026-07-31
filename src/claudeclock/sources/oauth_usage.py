"""Primary source: Anthropic's official usage endpoint.

`GET https://api.anthropic.com/api/oauth/usage` is the same endpoint Claude
Code's own `/usage` screen reads, authenticated with the OAuth token already on
this machine. It is a documented, read-only, first-party surface - no scraping,
no browser automation, no undocumented reverse engineering of the wire
protocol.

Response (fields we use)::

    {
      "five_hour": {"utilization": 6.0,
                    "resets_at": "2026-07-31T16:40:00.714082+00:00"},
      "seven_day": {...} | null,
      "limits": [{"kind": "session", "percent": 6, "is_active": true,
                  "resets_at": "..."}]
    }

`five_hour` is null when no session window is currently open - that is the
signal that the previous window has fully reset and nothing has re-opened one
yet.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ..auth import AuthError, Credentials, load_credentials, refresh_credentials
from . import Confidence, Snapshot, parse_timestamp

log = logging.getLogger("cclock.source.oauth")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "claudeclock/1.0"

# A 429 here is a minutes-scale limit, not a transient blip.
RATE_LIMIT_BACKOFF_MIN = 300.0    # 5 minutes
RATE_LIMIT_BACKOFF_MAX = 1800.0   # 30 minutes


class UsageEndpointError(RuntimeError):
    """The endpoint was reachable but did not give us a usable answer."""


class OAuthUsageSource:
    """Polls the official usage endpoint, with backoff and token recovery."""

    name = "oauth"

    def __init__(
        self,
        *,
        explicit_token: str | None = None,
        allow_refresh: bool = False,
        backoff_min: float = 5.0,
        backoff_max: float = 300.0,
        timeout: float = 20.0,
    ) -> None:
        self._explicit_token = explicit_token
        self._allow_refresh = allow_refresh
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
        )
        self._creds: Credentials | None = None
        self._consecutive_failures = 0
        self._blocked_until = 0.0
        self.last_error: str | None = None

    # -- credentials --------------------------------------------------------

    def _credentials(self, *, force_reload: bool = False) -> Credentials:
        if self._creds is None or force_reload:
            self._creds = load_credentials(self._explicit_token)

        if self._creds.is_expired:
            # Claude Code may have rotated the token already; re-read first.
            reloaded = load_credentials(self._explicit_token)
            if not reloaded.is_expired:
                self._creds = reloaded
            elif self._allow_refresh:
                self._creds = refresh_credentials(reloaded)
            else:
                raise AuthError(
                    "stored access token has expired. Run `claude` once to refresh "
                    "it, or set CLAUDECLOCK_ALLOW_TOKEN_REFRESH=true."
                )
        return self._creds

    # -- backoff ------------------------------------------------------------

    def _note_failure(self, reason: str) -> None:
        self._consecutive_failures += 1
        self.last_error = reason

        if "429" in reason or "rate limited" in reason.lower():
            # This endpoint is meant for occasional interactive use, so its
            # limit is measured in minutes, not seconds. Retrying on the
            # ordinary network backoff just re-trips it and keeps us pinned to
            # inferred data, so wait properly.
            delay = min(
                RATE_LIMIT_BACKOFF_MAX,
                RATE_LIMIT_BACKOFF_MIN * (2 ** (self._consecutive_failures - 1)),
            )
        else:
            delay = min(
                self._backoff_max,
                self._backoff_min * (2 ** (self._consecutive_failures - 1)),
            )
        delay *= 0.75 + random.random() * 0.5  # jitter, avoid lockstep retries
        self._blocked_until = time.monotonic() + delay
        log.warning(
            "usage endpoint unavailable, backing off",
            extra={
                "reason": reason,
                "failures": self._consecutive_failures,
                "retry_in": round(delay, 1),
            },
        )

    def _note_success(self) -> None:
        if self._consecutive_failures:
            log.info(
                "usage endpoint recovered",
                extra={"after_failures": self._consecutive_failures},
            )
        self._consecutive_failures = 0
        self._blocked_until = 0.0
        self.last_error = None

    @property
    def in_backoff(self) -> bool:
        return time.monotonic() < self._blocked_until

    # -- fetching -----------------------------------------------------------

    def fetch(self) -> Snapshot | None:
        if self.in_backoff:
            log.log(5, "skipping poll, in backoff")
            return None

        try:
            creds = self._credentials()
        except AuthError as exc:
            self._note_failure(f"auth: {exc}")
            return None

        try:
            payload = self._request(creds)
        except UsageEndpointError as exc:
            self._note_failure(str(exc))
            return None

        self._note_success()
        return self._to_snapshot(payload)

    def _request(self, creds: Credentials, *, _retried: bool = False) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {creds.access_token}",
            "anthropic-beta": OAUTH_BETA,
            "Content-Type": "application/json",
        }
        try:
            response = self._client.get(USAGE_URL, headers=headers)
        except httpx.TimeoutException as exc:
            raise UsageEndpointError(f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UsageEndpointError(f"network: {exc}") from exc

        if response.status_code == 401 and not _retried:
            # Token rotated underneath us; reload once and try again.
            log.info("usage endpoint returned 401, reloading credentials")
            try:
                fresh = self._credentials(force_reload=True)
            except AuthError as exc:
                raise UsageEndpointError(f"auth: {exc}") from exc
            return self._request(fresh, _retried=True)

        if response.status_code == 429:
            raise UsageEndpointError("rate limited by usage endpoint (429)")

        if response.status_code >= 400:
            raise UsageEndpointError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise UsageEndpointError(f"malformed JSON response: {exc}") from exc

        if not isinstance(data, dict):
            raise UsageEndpointError("unexpected response shape (not an object)")

        log.log(5, "usage payload", extra={"keys": sorted(data.keys())})
        return data

    # -- parsing ------------------------------------------------------------

    @staticmethod
    def _to_snapshot(payload: dict[str, Any]) -> Snapshot:
        five = payload.get("five_hour")
        resets_at: datetime | None = None
        utilization: float | None = None

        if isinstance(five, dict):
            resets_at = parse_timestamp(five.get("resets_at"))
            raw_util = five.get("utilization")
            if isinstance(raw_util, (int, float)):
                utilization = float(raw_util)

        # `limits[]` is the newer, richer view of the same data. Prefer it when
        # `five_hour` is absent but an active session limit is listed.
        if resets_at is None:
            for entry in payload.get("limits") or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("kind") == "session" and entry.get("is_active"):
                    resets_at = parse_timestamp(entry.get("resets_at"))
                    percent = entry.get("percent")
                    if utilization is None and isinstance(percent, (int, float)):
                        utilization = float(percent)
                    break

        weekly = payload.get("seven_day")
        weekly_util: float | None = None
        weekly_reset: datetime | None = None
        if isinstance(weekly, dict):
            weekly_reset = parse_timestamp(weekly.get("resets_at"))
            raw_weekly = weekly.get("utilization")
            if isinstance(raw_weekly, (int, float)):
                weekly_util = float(raw_weekly)

        # A window that has already elapsed is a stale reading, not an open
        # window; treat it as closed so the state machine re-arms.
        if resets_at is not None and resets_at <= datetime.now(timezone.utc):
            log.debug(
                "endpoint reported an already-elapsed window",
                extra={"resets_at": resets_at.isoformat()},
            )

        return Snapshot(
            resets_at=resets_at,
            utilization=utilization,
            weekly_utilization=weekly_util,
            weekly_resets_at=weekly_reset,
            source="oauth",
            confidence=Confidence.AUTHORITATIVE,
            raw={"five_hour": five, "limits": payload.get("limits")},
        )

    def close(self) -> None:
        self._client.close()
