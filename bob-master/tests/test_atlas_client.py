"""
Tests AtlasClient against a fake inner httpx client -- never hits the real
API in the test suite (confirmed working against the real one manually,
2026-08-04: 133 real accounts fetched).
"""
import time

from app.config import get_settings
from app.integrations.atlas_client import AtlasClient


class _FakeResponse:
    def __init__(self, payload, content=b"content"):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeHttpxClient:
    def __init__(self, payload, post_response=None):
        self._payload = payload
        self._post_response = post_response if post_response is not None else _FakeResponse({})
        self.requested_paths: list[str] = []
        self.posted: list[dict] = []

    def get(self, path):
        self.requested_paths.append(path)
        return _FakeResponse(self._payload)

    def post(self, path, json):
        self.posted.append({"path": path, "json": json})
        return self._post_response


def _client(monkeypatch) -> AtlasClient:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    return AtlasClient()


def test_get_all_accounts_returns_the_accounts_list(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient({"count": 2, "accounts": [{"companyName": "Acme Co"}, {"companyName": "Beta LLC"}]})
    client._client = fake

    accounts = client.get_all_accounts()

    assert [a["companyName"] for a in accounts] == ["Acme Co", "Beta LLC"]
    assert fake.requested_paths == ["/api/accounts"]


def test_get_all_accounts_handles_missing_accounts_key_gracefully(monkeypatch):
    client = _client(monkeypatch)
    client._client = _FakeHttpxClient({"count": 0})

    assert client.get_all_accounts() == []


def test_post_campaigns_posts_to_the_account_specific_path(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient({}, post_response=_FakeResponse({"ok": True}))
    client._client = fake

    payload = {"platform": "google", "adAccountId": "123-456-7890"}
    result = client.post_campaigns("atlas-id-1", payload)

    assert fake.posted == [{"path": "/api/accounts/atlas-id-1/campaigns", "json": payload}]
    assert result == {"ok": True}


def test_post_campaigns_handles_an_empty_response_body(monkeypatch):
    client = _client(monkeypatch)
    client._client = _FakeHttpxClient({}, post_response=_FakeResponse(None, content=b""))

    result = client.post_campaigns("atlas-id-1", {"platform": "meta"})

    assert result == {}


class _FlakyPostClient:
    """Raises on the first `fail_count` post() calls, then returns
    `success_response` -- for exercising post_pulse_run's retry loop, unlike
    _FakeHttpxClient's post() which always succeeds."""

    def __init__(self, fail_count, success_response=None):
        self.fail_count = fail_count
        self.success_response = success_response if success_response is not None else _FakeResponse({"ok": True})
        self.posted: list[dict] = []
        self.calls = 0

    def post(self, path, json):
        self.calls += 1
        self.posted.append({"path": path, "json": json})
        if self.calls <= self.fail_count:
            raise RuntimeError(f"boom on attempt {self.calls}")
        return self.success_response


def test_post_pulse_run_posts_to_the_pulse_runs_path(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient({}, post_response=_FakeResponse({"ok": True}))
    client._client = fake

    payload = {"runId": 1, "runAt": "2026-09-28T00:00:00+00:00", "count": 0, "accounts": []}
    result = client.post_pulse_run(payload)

    assert fake.posted == [{"path": "/api/pulse-runs", "json": payload}]
    assert result == {"ok": True}


def test_post_pulse_run_retries_and_succeeds_on_a_later_attempt(monkeypatch):
    client = _client(monkeypatch)
    flaky = _FlakyPostClient(fail_count=2)
    client._client = flaky
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    result = client.post_pulse_run({"runId": 1})

    assert flaky.calls == 3
    assert result == {"ok": True}
    assert sleep_calls == [1.0, 2.0]  # 2**0, 2**1 -- backoff between the two failed attempts


def test_post_pulse_run_gives_up_after_three_attempts_and_raises(monkeypatch):
    client = _client(monkeypatch)
    flaky = _FlakyPostClient(fail_count=99)
    client._client = flaky
    monkeypatch.setattr(time, "sleep", lambda s: None)

    try:
        client.post_pulse_run({"runId": 1})
        assert False, "expected post_pulse_run to raise after exhausting retries"
    except RuntimeError as exc:
        assert "boom on attempt 3" in str(exc)

    assert flaky.calls == 3
