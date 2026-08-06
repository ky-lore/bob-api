"""Same coverage as tests/test_atlas_client.py, against the adspend/ copy."""
from adspend.atlas_client import AtlasClient
from adspend.config import get_settings


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
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_SECRET", "x")
    monkeypatch.setenv("GOOGLE_ADS_REFRESH_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "3910981944")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    return AtlasClient()


def test_get_all_accounts_returns_the_accounts_list(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient({"count": 1, "accounts": [{"companyName": "Acme Co"}]})
    client._client = fake

    accounts = client.get_all_accounts()

    assert [a["companyName"] for a in accounts] == ["Acme Co"]
    assert fake.requested_paths == ["/api/accounts"]
