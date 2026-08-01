"""Credential parsing, expiry, file permissions, and token refresh.

These paths are security-sensitive (they read/write the same store Claude
Code owns, over the network on refresh) and had no coverage at all before
this file, despite several recent bugfix commits touching them.
"""

from __future__ import annotations

import json
import stat
import time

import httpx
import pytest

from claudeclock import auth as auth_module
from claudeclock.auth import AuthError, Credentials, _parse_blob, _write_file


# --------------------------------------------------------------------------
# Credentials.is_expired / expires_in
# --------------------------------------------------------------------------


def test_credentials_with_no_expiry_never_expire():
    creds = Credentials(access_token="tok")
    assert creds.is_expired is False
    assert creds.expires_in is None


def test_credentials_expired_in_the_past():
    creds = Credentials(access_token="tok", expires_at=time.time() - 10)
    assert creds.is_expired is True
    assert creds.expires_in == 0.0


def test_credentials_expiry_skew_treats_near_future_as_expired():
    """A token expiring in 30s is unusable - it must not be sent as fresh."""
    creds = Credentials(access_token="tok", expires_at=time.time() + 30)
    assert creds.is_expired is True


def test_credentials_comfortably_valid_is_not_expired():
    creds = Credentials(access_token="tok", expires_at=time.time() + 3600)
    assert creds.is_expired is False
    assert 3500 < creds.expires_in <= 3600


def test_redacted_never_exposes_the_full_token():
    creds = Credentials(access_token="sk-ant-oat01-verysecrettoken")
    redacted = creds.redacted()
    assert "verysecrettoken" not in redacted
    assert redacted.startswith("sk-ant-oat01-")


# --------------------------------------------------------------------------
# _parse_blob
# --------------------------------------------------------------------------


def test_parse_blob_rejects_invalid_json():
    assert _parse_blob("not json", "test") is None


def test_parse_blob_nested_claude_ai_oauth_shape():
    blob = json.dumps({"claudeAiOauth": {"accessToken": "tok123", "subscriptionType": "pro"}})
    creds = _parse_blob(blob, "keychain")
    assert creds.access_token == "tok123"
    assert creds.subscription_type == "pro"
    assert creds.source == "keychain"


def test_parse_blob_flat_shape_snake_case():
    blob = json.dumps({"access_token": "tok456", "refresh_token": "rtok"})
    creds = _parse_blob(blob, "file")
    assert creds.access_token == "tok456"
    assert creds.refresh_token == "rtok"


def test_parse_blob_missing_token_returns_none():
    blob = json.dumps({"claudeAiOauth": {"subscriptionType": "pro"}})
    assert _parse_blob(blob, "test") is None


def test_parse_blob_not_an_object_returns_none():
    assert _parse_blob("42", "test") is None
    assert _parse_blob("[1, 2, 3]", "test") is None


def test_parse_blob_expires_at_milliseconds_converted_to_seconds():
    epoch_ms = (time.time() + 3600) * 1000
    blob = json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": epoch_ms}})
    creds = _parse_blob(blob, "test")
    assert 3500 < creds.expires_in <= 3600


def test_parse_blob_expires_at_seconds_left_as_is():
    epoch_s = time.time() + 3600
    blob = json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": epoch_s}})
    creds = _parse_blob(blob, "test")
    assert 3500 < creds.expires_in <= 3600


def test_parse_blob_unparseable_expires_at_is_ignored_not_fatal():
    blob = json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": "not-a-number"}})
    creds = _parse_blob(blob, "test")
    assert creds.access_token == "tok"
    assert creds.expires_at is None


# --------------------------------------------------------------------------
# _write_file: 0600 permissions
# --------------------------------------------------------------------------


def test_write_file_creates_credentials_with_0600(tmp_path, monkeypatch):
    target = tmp_path / ".credentials.json"
    monkeypatch.setattr(auth_module, "CREDENTIALS_FILE", target)

    ok = _write_file(Credentials(access_token="secret-token", refresh_token="r1"))
    assert ok is True

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600

    saved = json.loads(target.read_text())
    assert saved["claudeAiOauth"]["accessToken"] == "secret-token"


def test_write_file_reasserts_0600_on_a_preexisting_looser_file(tmp_path, monkeypatch):
    target = tmp_path / ".credentials.json"
    target.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old"}}))
    target.chmod(0o644)
    monkeypatch.setattr(auth_module, "CREDENTIALS_FILE", target)

    _write_file(Credentials(access_token="new-token"))

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_file_preserves_other_keys_in_the_store(tmp_path, monkeypatch):
    target = tmp_path / ".credentials.json"
    target.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old"}, "otherApp": {"x": 1}}))
    monkeypatch.setattr(auth_module, "CREDENTIALS_FILE", target)

    _write_file(Credentials(access_token="new-token"))

    saved = json.loads(target.read_text())
    assert saved["otherApp"] == {"x": 1}
    assert saved["claudeAiOauth"]["accessToken"] == "new-token"


# --------------------------------------------------------------------------
# load_credentials
# --------------------------------------------------------------------------


def test_load_credentials_prefers_explicit_token(monkeypatch):
    monkeypatch.setattr(auth_module, "_read_keychain", lambda: Credentials(access_token="from-keychain"))
    monkeypatch.setattr(auth_module, "_read_file", lambda: Credentials(access_token="from-file"))

    creds = auth_module.load_credentials(explicit_token="explicit")
    assert creds.access_token == "explicit"
    assert creds.source == "config"


def test_load_credentials_falls_back_to_file_when_no_keychain(monkeypatch):
    monkeypatch.setattr(auth_module, "_read_keychain", lambda: None)
    monkeypatch.setattr(auth_module, "_read_file", lambda: Credentials(access_token="from-file", source="file"))

    creds = auth_module.load_credentials()
    assert creds.access_token == "from-file"


def test_load_credentials_raises_when_nothing_found(monkeypatch):
    monkeypatch.setattr(auth_module, "_read_keychain", lambda: None)
    monkeypatch.setattr(auth_module, "_read_file", lambda: None)

    with pytest.raises(AuthError):
        auth_module.load_credentials()


# --------------------------------------------------------------------------
# refresh_credentials
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload=None, status_error=None, json_error=None):
        self._payload = payload
        self._status_error = status_error
        self._json_error = json_error

    def raise_for_status(self):
        if self._status_error:
            raise self._status_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


def test_refresh_credentials_requires_a_refresh_token():
    creds = Credentials(access_token="tok", refresh_token=None)
    with pytest.raises(AuthError, match="no refresh token"):
        auth_module.refresh_credentials(creds)


def test_refresh_credentials_success_updates_token_and_expiry(monkeypatch):
    creds = Credentials(access_token="old", refresh_token="rtok", source="keychain")
    response = _FakeResponse({"access_token": "new-tok", "expires_in": 3600})
    monkeypatch.setattr(auth_module.httpx, "post", lambda *a, **k: response)

    refreshed = auth_module.refresh_credentials(creds)
    assert refreshed.access_token == "new-tok"
    assert 3500 < refreshed.expires_in <= 3600


def test_refresh_credentials_persists_when_source_is_file(monkeypatch, tmp_path):
    target = tmp_path / ".credentials.json"
    monkeypatch.setattr(auth_module, "CREDENTIALS_FILE", target)
    creds = Credentials(access_token="old", refresh_token="rtok", source="file")
    response = _FakeResponse({"access_token": "new-tok", "expires_in": 3600})
    monkeypatch.setattr(auth_module.httpx, "post", lambda *a, **k: response)

    auth_module.refresh_credentials(creds)

    assert json.loads(target.read_text())["claudeAiOauth"]["accessToken"] == "new-tok"


def test_refresh_credentials_does_not_persist_when_source_is_keychain(monkeypatch, tmp_path):
    """We deliberately never write to the store Claude Code owns via Keychain."""
    target = tmp_path / ".credentials.json"
    monkeypatch.setattr(auth_module, "CREDENTIALS_FILE", target)
    creds = Credentials(access_token="old", refresh_token="rtok", source="keychain")
    response = _FakeResponse({"access_token": "new-tok", "expires_in": 3600})
    monkeypatch.setattr(auth_module.httpx, "post", lambda *a, **k: response)

    auth_module.refresh_credentials(creds)

    assert not target.exists()


def test_refresh_credentials_wraps_http_errors(monkeypatch):
    creds = Credentials(access_token="old", refresh_token="rtok")
    response = _FakeResponse(status_error=httpx.HTTPStatusError("bad", request=None, response=None))
    monkeypatch.setattr(auth_module.httpx, "post", lambda *a, **k: response)

    with pytest.raises(AuthError, match="token refresh failed"):
        auth_module.refresh_credentials(creds)


def test_refresh_credentials_wraps_non_json_body(monkeypatch):
    """A captive portal or proxy can return an HTML body instead of JSON."""
    creds = Credentials(access_token="old", refresh_token="rtok")
    response = _FakeResponse(json_error=ValueError("Expecting value"))
    monkeypatch.setattr(auth_module.httpx, "post", lambda *a, **k: response)

    with pytest.raises(AuthError, match="non-JSON body"):
        auth_module.refresh_credentials(creds)


def test_refresh_credentials_rejects_response_missing_access_token(monkeypatch):
    creds = Credentials(access_token="old", refresh_token="rtok")
    response = _FakeResponse({"token_type": "bearer"})
    monkeypatch.setattr(auth_module.httpx, "post", lambda *a, **k: response)

    with pytest.raises(AuthError, match="no access_token"):
        auth_module.refresh_credentials(creds)


def test_refresh_credentials_rejects_non_object_response(monkeypatch):
    creds = Credentials(access_token="old", refresh_token="rtok")
    response = _FakeResponse(["unexpected", "list"])
    monkeypatch.setattr(auth_module.httpx, "post", lambda *a, **k: response)

    with pytest.raises(AuthError, match="not a JSON object"):
        auth_module.refresh_credentials(creds)
