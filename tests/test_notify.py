"""Webhook delivery: a bad URL must fail the channel, not the dispatch."""

from __future__ import annotations

import httpx

from claudeclock.config import Config
from claudeclock.notify import Notifier, _post

# A URL that is malformed rather than merely unreachable. httpx raises
# InvalidURL for it before any connection is attempted, so this stays
# hermetic - no socket is opened.
MALFORMED_URL = "http://example.com:port/hook"


def test_post_reports_a_malformed_url_as_a_failed_send():
    assert _post(MALFORMED_URL, {}, 1.0, "webhook") is False


def test_post_reports_a_transport_error_as_a_failed_send(monkeypatch):
    def boom(*_args, **_kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", boom)
    assert _post("https://example.com/hook", {}, 1.0, "webhook") is False


def test_test_reports_a_malformed_webhook_instead_of_raising():
    """`cclock test` should name the broken channel, not exit with a traceback."""
    config = Config(desktop_notifications=False, webhook_url=MALFORMED_URL)
    assert Notifier(config).test() == {"webhook": False}


def test_one_broken_channel_does_not_stop_the_others(monkeypatch):
    """A malformed Discord URL used to abort the dispatch before Slack ran."""
    posted: list[str] = []

    def record(url, *_args, **_kwargs):
        # Stand in for httpx's own parsing: the malformed URL never reaches
        # the network, it raises before a connection is attempted.
        if url == MALFORMED_URL:
            raise httpx.InvalidURL("Invalid port: 'port'")
        posted.append(url)
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", record)

    config = Config(
        desktop_notifications=False,
        discord_webhook_url=MALFORMED_URL,
        slack_webhook_url="https://hooks.slack.com/services/x",
        webhook_url="https://example.com/hook",
    )
    results = Notifier(config).test()

    assert results["discord"] is False
    assert results["slack"] is True
    assert results["webhook"] is True
