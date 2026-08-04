"""
Tests AtlasClient against a fake inner httpx client -- never hits the real
API in the test suite (confirmed working against the real one manually,
2026-08-04: 133 real accounts fetched).
"""
from app.config import get_settings
from app.integrations.atlas_client import AtlasClient


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeHttpxClient:
    def __init__(self, payload):
        self._payload = payload
        self.requested_paths: list[str] = []

    def get(self, path):
        self.requested_paths.append(path)
        return _FakeResponse(self._payload)


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
