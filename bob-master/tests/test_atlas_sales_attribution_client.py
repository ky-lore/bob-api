"""AtlasClient.set_sales_attribution -- request shape + error mapping, against a fake inner httpx client."""
import pytest

from app.config import get_settings
from app.integrations.atlas_client import AtlasClient, AtlasWriteError


class _Resp:
    def __init__(self, status=200, payload=None, text="", content=b"x"):
        self.status_code, self._payload, self.text, self.content = status, payload, text, content

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeHttpx:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def patch(self, path, json):
        self.calls.append((path, json))
        return self.resp


def _client(monkeypatch, resp):
    for k, v in {"DATABASE_URL": "sqlite:///:memory:", "GHL_API_KEY": "x", "GHL_LOCATION_ID": "x", "CLICKUP_API_TOKEN": "x",
                 "SLACK_BOT_TOKEN": "x", "GOOGLE_SERVICE_ACCOUNT_JSON_B64": "eyJ9", "ANTHROPIC_API_KEY": "x", "ATLAS_API_KEY": "x"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    c = AtlasClient()
    c._client = _FakeHttpx(resp)
    return c


def test_sends_only_the_fields_given_plus_actor(monkeypatch):
    c = _client(monkeypatch, _Resp(payload={"changed": ["salesRep"]}))
    out = c.set_sales_attribution("abc123", actor="Bob", sales_rep="kylekellner@advancedmarketers.co")
    assert c._client.calls == [("/api/accounts/abc123/sales-attribution", {"actor": "Bob", "salesRep": "kylekellner@advancedmarketers.co"})]
    assert out == {"changed": ["salesRep"]}


def test_none_means_clear_and_omitted_means_leave_alone(monkeypatch):
    c = _client(monkeypatch, _Resp(payload={}))
    c.set_sales_attribution("a", actor="Bob", referral_partner=None)
    assert c._client.calls[0][1] == {"actor": "Bob", "referralPartner": None}          # explicit null
    c.set_sales_attribution("a", actor="Bob", sales_rep="x@y.co", referral_partner="Acme")
    assert c._client.calls[1][1] == {"actor": "Bob", "salesRep": "x@y.co", "referralPartner": "Acme"}


def test_rejections_carry_atlas_status_and_message(monkeypatch):
    c = _client(monkeypatch, _Resp(status=422, payload={"message": 'No active user matches salesRep "zed@x.co"'}))
    with pytest.raises(AtlasWriteError) as e:
        c.set_sales_attribution("a", actor="Bob", sales_rep="zed@x.co")
    assert e.value.status == 422 and "No active user matches" in e.value.message


def test_non_json_error_body_still_raises_cleanly(monkeypatch):
    c = _client(monkeypatch, _Resp(status=502, payload=None, text="Bad gateway"))
    with pytest.raises(AtlasWriteError) as e:
        c.set_sales_attribution("a", actor="Bob", sales_rep="x@y.co")
    assert e.value.status == 502 and e.value.message == "Bad gateway"
