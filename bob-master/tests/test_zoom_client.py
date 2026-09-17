"""
Tests ZoomClient against a fake inner httpx client -- never hits the real
Zoom API in the test suite (confirmed working against the real one
manually, 2026-09-17: real token mint, real users/meetings/recordings pull,
real transcript download). Covers: account_credentials token minting +
caching, list_users/list_user_meetings/list_recordings_for_user pagination,
transcript download headers, and the required-settings guard.
"""
import time

import pytest

from app.config import get_settings
from app.integrations.zoom_client import ZoomClient


class _FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeHttpxClient:
    def __init__(self, get_pages=None, token_response=None):
        self.get_pages = get_pages or []  # consumed in order for successive GETs
        self._token_response = token_response or _FakeResponse({"access_token": "fake-token", "expires_in": 3600})
        self.token_requests: list[dict] = []
        self.get_requests: list[dict] = []

    def post(self, url, params=None, auth=None):
        self.token_requests.append({"url": url, "params": params, "auth": auth})
        return self._token_response

    def get(self, url, params=None, headers=None, follow_redirects=None):
        self.get_requests.append({"url": url, "params": params, "headers": headers, "follow_redirects": follow_redirects})
        return self.get_pages.pop(0)


def _client(monkeypatch) -> ZoomClient:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    monkeypatch.setenv("ZOOM_ACCOUNT_ID", "acc-1")
    monkeypatch.setenv("ZOOM_CLIENT_ID", "client-1")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "secret-1")
    get_settings.cache_clear()
    return ZoomClient()


def test_missing_settings_raises_a_clear_error(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    # Explicit empty strings, not delenv -- the real .env file (this repo's
    # local dev secrets) has real values for these now, and pydantic-settings
    # falls back to reading the file when the process env var is merely
    # absent rather than overridden.
    monkeypatch.setenv("ZOOM_ACCOUNT_ID", "")
    monkeypatch.setenv("ZOOM_CLIENT_ID", "")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="ZOOM_ACCOUNT_ID"):
        ZoomClient()


def test_access_token_is_minted_via_account_credentials_grant(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient()
    client._client = fake

    token = client._get_access_token()

    assert token == "fake-token"
    assert len(fake.token_requests) == 1
    req = fake.token_requests[0]
    assert req["params"] == {"grant_type": "account_credentials", "account_id": "acc-1"}
    assert req["auth"] == ("client-1", "secret-1")


def test_access_token_is_cached_until_near_expiry(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient()
    client._client = fake

    client._get_access_token()
    client._get_access_token()

    assert len(fake.token_requests) == 1  # second call reused the cached token


def test_access_token_is_reminted_once_expired(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient()
    client._client = fake

    client._get_access_token()
    client._token_expires_at = time.time() - 1  # force expiry
    client._get_access_token()

    assert len(fake.token_requests) == 2


def test_list_users_paginates_through_next_page_token(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient(get_pages=[
        _FakeResponse({"users": [{"email": "a@x.com"}], "next_page_token": "p2"}),
        _FakeResponse({"users": [{"email": "b@x.com"}], "next_page_token": ""}),
    ])
    client._client = fake

    users = client.list_users()

    assert [u["email"] for u in users] == ["a@x.com", "b@x.com"]
    assert fake.get_requests[0]["url"] == "https://api.zoom.us/v2/users"
    assert "next_page_token" not in fake.get_requests[0]["params"]
    assert fake.get_requests[1]["params"]["next_page_token"] == "p2"


def test_list_user_meetings_hits_the_report_endpoint(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient(get_pages=[
        _FakeResponse({"meetings": [{"topic": "AM x Acme Co"}], "next_page_token": ""}),
    ])
    client._client = fake

    meetings = client.list_user_meetings("tim@x.com", "2026-09-16", "2026-09-17")

    assert meetings == [{"topic": "AM x Acme Co"}]
    assert fake.get_requests[0]["url"] == "https://api.zoom.us/v2/report/users/tim@x.com/meetings"
    assert fake.get_requests[0]["params"]["from"] == "2026-09-16"
    assert fake.get_requests[0]["params"]["to"] == "2026-09-17"


def test_list_recordings_for_user_hits_the_recordings_endpoint(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient(get_pages=[
        _FakeResponse({"meetings": [{"uuid": "u1", "recording_files": []}], "next_page_token": ""}),
    ])
    client._client = fake

    recordings = client.list_recordings_for_user("tim@x.com", "2026-09-16", "2026-09-17")

    assert recordings == [{"uuid": "u1", "recording_files": []}]
    assert fake.get_requests[0]["url"] == "https://api.zoom.us/v2/users/tim@x.com/recordings"


def test_get_transcript_text_follows_redirects_with_bearer_auth(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient(get_pages=[_FakeResponse(text="WEBVTT\n\nHello there")])
    client._client = fake

    text = client.get_transcript_text("https://zoom.us/rec/download/abc")

    assert text == "WEBVTT\n\nHello there"
    req = fake.get_requests[0]
    assert req["url"] == "https://zoom.us/rec/download/abc"
    assert req["follow_redirects"] is True
    assert req["headers"]["Authorization"] == "Bearer fake-token"
