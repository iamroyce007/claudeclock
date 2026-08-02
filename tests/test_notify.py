"""Notification fan-out: channel isolation and webhook payloads."""

from __future__ import annotations

import httpx
import pytest

from claudeclock import notify
from claudeclock.config import Config
from claudeclock.notify import Notification, Notifier, dispatch


def config_for(tmp_path, **kwargs) -> Config:
    defaults = dict(
        state_dir=tmp_path,
        desktop_notifications=False,
        webhook_timeout=1.0,
    )
    defaults.update(kwargs)
    return Config(**defaults)


NOTE = Notification(title="Window closing", message="5 minutes left", level="warning")


# --------------------------------------------------------------------------
# channel isolation
# --------------------------------------------------------------------------


def test_a_url_httpx_refuses_to_parse_is_a_failed_channel(tmp_path, monkeypatch):
    """InvalidURL descends from Exception, not HTTPError.

    A mistyped port used to escape `_post` entirely instead of being reported
    as a dead channel.
    """
    calls: list[str] = []

    def fake_post(url, **kwargs):
        calls.append(url)
        raise httpx.InvalidURL("invalid port")

    monkeypatch.setattr(notify.httpx, "post", fake_post)

    results = dispatch(config_for(tmp_path, webhook_url="https://host:8O80/hook"), NOTE)

    assert results == {"webhook": False}
    assert calls == ["https://host:8O80/hook"]


def test_post_swallows_invalid_url_itself(monkeypatch):
    """Pinned separately from dispatch, which would mask it either way.

    `_isolated` is a backstop; a URL error is an ordinary dead channel and
    belongs to `_post`, which reports it as such rather than as a channel that
    escaped and had to be caught.
    """

    def fake_post(url, **kwargs):
        raise httpx.InvalidURL("invalid port")

    monkeypatch.setattr(notify.httpx, "post", fake_post)

    assert notify._post("https://host:8O80/h", {}, 1.0, "webhook") is False


def test_one_broken_channel_does_not_skip_the_others(tmp_path, monkeypatch):
    """Discord is dispatched first; its failure must not strand slack/webhook."""
    seen: list[str] = []

    def fake_post(url, **kwargs):
        seen.append(url)
        if "discord" in url:
            raise httpx.InvalidURL("invalid port")
        return httpx.Response(200)

    monkeypatch.setattr(notify.httpx, "post", fake_post)

    results = dispatch(
        config_for(
            tmp_path,
            discord_webhook_url="https://discord.test:8O80/hook",
            slack_webhook_url="https://slack.test/hook",
            webhook_url="https://generic.test/hook",
        ),
        NOTE,
    )

    assert results == {"discord": False, "slack": True, "webhook": True}
    assert len(seen) == 3, "delivery stopped at the first broken channel"


def test_a_desktop_backend_that_raises_does_not_strand_the_webhooks(tmp_path, monkeypatch):
    def boom(note):
        raise OSError("no notification daemon")

    monkeypatch.setattr(notify, "send_desktop", boom)
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: httpx.Response(200))

    results = dispatch(
        config_for(tmp_path, desktop_notifications=True, webhook_url="https://x.test/h"),
        NOTE,
    )

    assert results == {"desktop": False, "webhook": True}


def test_a_rejected_webhook_reports_false(tmp_path, monkeypatch):
    monkeypatch.setattr(
        notify.httpx, "post", lambda url, **kw: httpx.Response(404, text="nope")
    )

    results = dispatch(config_for(tmp_path, webhook_url="https://x.test/h"), NOTE)

    assert results == {"webhook": False}


def test_a_transport_failure_reports_false(tmp_path, monkeypatch):
    def fake_post(url, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(notify.httpx, "post", fake_post)

    results = dispatch(config_for(tmp_path, webhook_url="https://x.test/h"), NOTE)

    assert results == {"webhook": False}


def test_no_configured_channels_yields_no_results(tmp_path):
    assert dispatch(config_for(tmp_path), NOTE) == {}


# --------------------------------------------------------------------------
# payloads
# --------------------------------------------------------------------------


def test_each_channel_gets_its_own_payload_shape(tmp_path, monkeypatch):
    sent: dict[str, dict] = {}

    def fake_post(url, *, json, **kwargs):
        sent[url] = json
        return httpx.Response(200)

    monkeypatch.setattr(notify.httpx, "post", fake_post)

    dispatch(
        config_for(
            tmp_path,
            discord_webhook_url="https://discord.test/h",
            slack_webhook_url="https://slack.test/h",
            webhook_url="https://generic.test/h",
        ),
        Notification(title="T", message="M", level="warning", fields={"left": "5m"}),
    )

    discord = sent["https://discord.test/h"]["embeds"][0]
    assert discord["description"] == "M"
    assert discord["color"] == 0xF39C12
    assert discord["fields"] == [{"name": "left", "value": "5m", "inline": True}]

    slack = sent["https://slack.test/h"]
    assert slack["text"] == "T: M"
    assert slack["blocks"][0]["text"]["text"].endswith("*\nM")

    generic = sent["https://generic.test/h"]
    assert generic["source"] == "claudeclock"
    assert generic["level"] == "warning"
    assert generic["fields"] == {"left": "5m"}


@pytest.mark.parametrize(
    "level,colour", [("info", 0x5865F2), ("success", 0x2ECC71),
                     ("warning", 0xF39C12), ("error", 0xE74C3C), ("bogus", 0x5865F2)]
)
def test_colour_falls_back_for_an_unknown_level(level, colour):
    assert Notification(title="t", message="m", level=level).colour == colour


# --------------------------------------------------------------------------
# test-notify parity
# --------------------------------------------------------------------------


def test_test_notify_reports_every_channel_past_a_broken_one(tmp_path, monkeypatch):
    """`cclock test-notify` used to abort on the first channel that raised."""

    def fake_post(url, **kwargs):
        if "discord" in url:
            raise httpx.InvalidURL("invalid port")
        return httpx.Response(200)

    monkeypatch.setattr(notify.httpx, "post", fake_post)

    results = Notifier(
        config_for(
            tmp_path,
            discord_webhook_url="https://discord.test:8O80/h",
            webhook_url="https://generic.test/h",
        )
    ).test()

    assert results == {"discord": False, "webhook": True}
