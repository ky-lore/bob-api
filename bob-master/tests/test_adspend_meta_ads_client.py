"""
Tests MetaAdsClient against a fake inner httpx client -- never hits the real
Meta Graph API in the test suite. Covers: campaigns+insights merge (a
campaign with no insights row gets zero metrics, not omitted), status
normalization (ACTIVE -> ENABLED), ctr percent-to-fraction conversion, and
the date_range handling (date_preset vs LAST_N_DAYS time_range).
"""
import json

import pytest

from adspend.config import get_settings
from adspend.meta_ads_client import MetaAdsClient


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeHttpxClient:
    """Dispatches by URL path suffix (campaigns vs insights)."""

    def __init__(self, campaigns_payload, insights_payload):
        self.campaigns_payload = campaigns_payload
        self.insights_payload = insights_payload
        self.requests: list[dict] = []

    def get(self, url, params=None, headers=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        if url.endswith("/campaigns"):
            return _FakeResponse(self.campaigns_payload)
        if url.endswith("/insights"):
            return _FakeResponse(self.insights_payload)
        raise AssertionError(f"unexpected URL: {url}")


def _client(monkeypatch) -> MetaAdsClient:
    monkeypatch.setenv("META_ACCESS_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_SECRET", "x")
    monkeypatch.setenv("GOOGLE_ADS_REFRESH_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "1234567890")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    return MetaAdsClient()


def test_get_account_spend_merges_insights_into_campaigns_by_id(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient(
        campaigns_payload={"data": [
            {"id": "1", "name": "Leads", "status": "ACTIVE", "effective_status": "ACTIVE", "objective": "OUTCOME_LEADS"},
            {"id": "2", "name": "Paused Ad", "status": "PAUSED", "effective_status": "PAUSED", "objective": "OUTCOME_LEADS"},
        ]},
        insights_payload={"data": [
            {"campaign_id": "1", "spend": "140.33", "impressions": "4095", "clicks": "42", "ctr": "1.025641", "cpc": "3.34119"},
        ]},
    )
    client._client = fake

    result = client.get_account_spend("act_123", date_range="LAST_7_DAYS")

    assert result["ad_account_id"] == "act_123"
    assert result["total_cost"] == 140.33
    assert result["total_impressions"] == 4095
    assert result["enabled_campaign_count"] == 1

    campaigns_by_id = {c["id"]: c for c in result["campaigns"]}
    # Campaign with an insights row
    assert campaigns_by_id["1"]["status"] == "ENABLED"  # normalized from ACTIVE
    assert campaigns_by_id["1"]["cost"] == 140.33
    assert campaigns_by_id["1"]["ctr"] == pytest.approx(0.01025641)  # percent -> fraction
    assert campaigns_by_id["1"]["avg_cpc"] == 3.34119
    assert campaigns_by_id["1"]["conversions"] == 0.0  # never computed from `actions`

    # Campaign with NO insights row -- zero metrics, not omitted from the list.
    assert campaigns_by_id["2"]["status"] == "PAUSED"
    assert campaigns_by_id["2"]["cost"] == 0.0
    assert campaigns_by_id["2"]["impressions"] == 0


def test_date_preset_used_for_known_ranges(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient(campaigns_payload={"data": []}, insights_payload={"data": []})
    client._client = fake

    client.get_account_spend("act_123", date_range="LAST_14_DAYS")

    insights_request = next(r for r in fake.requests if r["url"].endswith("/insights"))
    assert insights_request["params"]["date_preset"] == "last_14d"
    assert "time_range" not in insights_request["params"]


def test_last_n_days_uses_explicit_time_range(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient(campaigns_payload={"data": []}, insights_payload={"data": []})
    client._client = fake

    client.get_account_spend("act_123", date_range="LAST_10_DAYS")

    insights_request = next(r for r in fake.requests if r["url"].endswith("/insights"))
    assert "time_range" in insights_request["params"]
    time_range = json.loads(insights_request["params"]["time_range"])
    assert "since" in time_range and "until" in time_range


def test_invalid_date_range_is_rejected(monkeypatch):
    client = _client(monkeypatch)
    client._client = _FakeHttpxClient(campaigns_payload={"data": []}, insights_payload={"data": []})

    with pytest.raises(ValueError):
        client.get_account_spend("act_123", date_range="LAST_QUARTER")


def test_access_token_is_sent_via_authorization_header_not_query_param(monkeypatch):
    # Real bug, 2026-08-10: an access_token query param ends up embedded in
    # the request URL, and httpx's default HTTPStatusError.__str__ includes
    # the full URL -- so a query-param token leaks in plaintext into every
    # error message, which flows into ad_platform_errors/context_gather_json
    # and gets persisted to the database. The header form never does this.
    client = _client(monkeypatch)
    fake = _FakeHttpxClient(campaigns_payload={"data": []}, insights_payload={"data": []})
    client._client = fake

    client.get_account_spend("act_123")

    for r in fake.requests:
        assert r["headers"]["Authorization"] == "Bearer x"
        assert "access_token" not in (r["params"] or {})
