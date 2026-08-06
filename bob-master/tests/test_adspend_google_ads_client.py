"""
Tests GoogleAdsClient against a fake inner httpx client -- never hits the
real Google Ads API in the test suite. Covers: access-token minting +
caching, spend aggregation (micros -> dollars, enabled-campaign count), GAQL
date-range pagination, and the date_range allow-list.
"""
import pytest

from adspend.config import get_settings
from adspend.google_ads_client import GoogleAdsClient, filter_relevant_campaigns


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeHttpxClient:
    """Dispatches by URL since the real client uses one httpx.Client for both
    the OAuth token endpoint and the Ads API search endpoint."""

    def __init__(self, search_pages):
        self.search_pages = search_pages  # list of payload dicts, consumed in order
        self.token_requests: list[dict] = []
        self.search_requests: list[dict] = []

    def post(self, url, data=None, headers=None, json=None):
        if "oauth2.googleapis.com" in url:
            self.token_requests.append(data)
            return _FakeResponse({"access_token": "fake-token-1", "expires_in": 3600})
        self.search_requests.append({"url": url, "headers": headers, "json": json})
        return _FakeResponse(self.search_pages.pop(0))


def _client(monkeypatch) -> GoogleAdsClient:
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_SECRET", "x")
    monkeypatch.setenv("GOOGLE_ADS_REFRESH_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "391-098-1944")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    return GoogleAdsClient()


def test_get_account_spend_sums_cost_and_counts_enabled_campaigns(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient([{
        "results": [
            {
                "campaign": {"id": "1", "name": "Search", "status": "ENABLED", "advertisingChannelType": "SEARCH"},
                "metrics": {
                    "costMicros": "5000000", "impressions": "1000", "clicks": "50", "ctr": 0.05,
                    "averageCpc": 100000, "conversions": 4, "costPerConversion": 1250000, "conversionsValue": 200,
                },
            },
            # Display campaign has NO clicks/ctr/etc in the payload at all -- Google omits zero-valued
            # fields entirely (confirmed against real data), not just this test being lazy.
            {"campaign": {"id": "2", "name": "Display", "status": "PAUSED"}, "metrics": {"costMicros": "2500000"}},
        ]
    }])
    client._client = fake

    result = client.get_account_spend("123-456-7890", date_range="YESTERDAY")

    assert result["customer_id"] == "1234567890"
    assert result["total_cost"] == 7.5
    assert result["total_impressions"] == 1000
    assert result["total_clicks"] == 50
    assert result["total_conversions"] == 4
    assert result["total_conversions_value"] == 200
    assert result["enabled_campaign_count"] == 1
    assert result["campaigns"][0] == {
        "id": "1", "name": "Search", "status": "ENABLED", "channel_type": "SEARCH",
        "cost": 5.0, "impressions": 1000, "clicks": 50, "ctr": 0.05, "avg_cpc": 0.1,
        "conversions": 4, "cost_per_conversion": 1.25, "conversions_value": 200,
    }
    # Fields absent from the real payload default to 0, not a KeyError.
    assert result["campaigns"][1] == {
        "id": "2", "name": "Display", "status": "PAUSED", "channel_type": None,
        "cost": 2.5, "impressions": 0, "clicks": 0, "ctr": 0.0, "avg_cpc": 0.0,
        "conversions": 0.0, "cost_per_conversion": 0.0, "conversions_value": 0.0,
    }
    # login-customer-id header always carries the shared MCC, regardless of the target customer_id
    assert fake.search_requests[0]["headers"]["login-customer-id"] == "3910981944"


def test_get_account_spend_paginates_through_next_page_token(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient([
        {"results": [{"campaign": {"id": "1", "status": "ENABLED"}, "metrics": {"costMicros": "1000000"}}], "nextPageToken": "p2"},
        {"results": [{"campaign": {"id": "2", "status": "ENABLED"}, "metrics": {"costMicros": "2000000"}}]},
    ])
    client._client = fake

    result = client.get_account_spend("1234567890")

    assert result["total_cost"] == 3.0
    assert len(fake.search_requests) == 2
    assert fake.search_requests[1]["json"]["pageToken"] == "p2"


def test_access_token_is_cached_across_calls_not_re_minted(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient([
        {"results": []},
        {"results": []},
    ])
    client._client = fake

    client.get_account_spend("1234567890")
    client.get_account_spend("1234567890")

    assert len(fake.token_requests) == 1


def test_last_n_days_builds_an_explicit_between_clause(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpxClient([{"results": []}])
    client._client = fake

    client.get_account_spend("1234567890", date_range="LAST_10_DAYS")

    query = fake.search_requests[0]["json"]["query"]
    assert "BETWEEN" in query
    assert "DURING" not in query


def test_invalid_last_n_days_shape_is_rejected(monkeypatch):
    client = _client(monkeypatch)
    client._client = _FakeHttpxClient([])

    with pytest.raises(ValueError):
        client.get_account_spend("1234567890", date_range="LAST_DAYS")


def test_invalid_date_range_is_rejected_before_any_request(monkeypatch):
    client = _client(monkeypatch)
    client._client = _FakeHttpxClient([])

    with pytest.raises(ValueError):
        client.get_account_spend("1234567890", date_range="LAST_QUARTER")


def _campaign(status="REMOVED", cost=0.0, impressions=0, clicks=0):
    return {"id": "1", "name": "x", "status": status, "channel_type": "SEARCH", "cost": cost,
            "impressions": impressions, "clicks": clicks, "ctr": 0.0, "avg_cpc": 0.0,
            "conversions": 0.0, "cost_per_conversion": 0.0, "conversions_value": 0.0}


def test_filter_relevant_campaigns_keeps_enabled_regardless_of_activity():
    campaigns = [_campaign(status="ENABLED", cost=0.0, impressions=0, clicks=0)]
    assert filter_relevant_campaigns(campaigns) == campaigns


def test_filter_relevant_campaigns_keeps_inactive_campaigns_with_recent_activity():
    campaigns = [_campaign(status="PAUSED", cost=12.5)]
    assert filter_relevant_campaigns(campaigns) == campaigns

    campaigns = [_campaign(status="REMOVED", impressions=5)]
    assert filter_relevant_campaigns(campaigns) == campaigns

    campaigns = [_campaign(status="PAUSED", clicks=1)]
    assert filter_relevant_campaigns(campaigns) == campaigns


def test_filter_relevant_campaigns_drops_dead_campaigns_with_zero_activity():
    campaigns = [_campaign(status="REMOVED"), _campaign(status="PAUSED")]
    assert filter_relevant_campaigns(campaigns) == []
