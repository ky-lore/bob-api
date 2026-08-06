"""
Tests app.tasks.atlas_report.build_atlas_report with every external client
faked -- proves the atlas_id passthrough, the compressed google_ads shape
(enabled campaigns only, not full REMOVED history), the status/recent_work
split reaching each record, and soft-fail behavior when a customer_id is bad.
"""
from datetime import datetime, timedelta, timezone

import app.tasks.atlas_report as mod


def _atlas_account(company_name, *, atlas_id=None, google_ads_customer_id=None, clickup_folder_id=None, slack_channel_id=None):
    created_at = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "id": atlas_id or company_name.lower().replace(" ", "-"),
        "companyName": company_name,
        "stage": "live",
        "isActive": True,
        "createdAt": created_at,
        "integrations": {
            "clickupFolderId": clickup_folder_id,
            "internalSlackChannelId": slack_channel_id,
            "googleMccId": google_ads_customer_id,
        },
    }


class _FakeAtlasClient:
    accounts: list = []

    def get_all_accounts(self):
        return _FakeAtlasClient.accounts


class _FakeClickUp:
    def get_folder_lists(self, folder_id):
        return []


class _FakeSlack:
    def channel_history(self, channel_id, oldest_ts=None):
        return []


class _FakeGoogleAdsClient:
    responses: dict = {}

    def get_account_spend(self, customer_id, date_range="YESTERDAY"):
        if customer_id not in _FakeGoogleAdsClient.responses:
            raise RuntimeError(f"no fake response for {customer_id}")
        return _FakeGoogleAdsClient.responses[customer_id]


def _spend(customer_id, total_cost, campaigns):
    return {
        "customer_id": customer_id,
        "date_range": "LAST_10_DAYS",
        "total_cost": total_cost,
        "total_impressions": 100,
        "total_clicks": 5,
        "total_conversions": 1.0,
        "total_conversions_value": 1.0,
        "enabled_campaign_count": sum(1 for c in campaigns if c["status"] == "ENABLED"),
        "campaigns": campaigns,
    }


def _setup(monkeypatch):
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    _FakeAtlasClient.accounts = []
    _FakeGoogleAdsClient.responses = {}


def test_record_carries_the_atlas_id_and_compressed_google_ads_summary(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Acme Co", atlas_id="acme-123", google_ads_customer_id="1234567890")]
    _FakeGoogleAdsClient.responses = {
        "1234567890": _spend("1234567890", 50.0, [
            {"id": "1", "name": "Active", "status": "ENABLED", "channel_type": "SEARCH", "cost": 50.0,
             "impressions": 100, "clicks": 5, "ctr": 0.05, "avg_cpc": 10.0, "conversions": 1.0,
             "cost_per_conversion": 50.0, "conversions_value": 1.0},
            {"id": "2", "name": "Dead", "status": "REMOVED", "channel_type": "SEARCH", "cost": 0.0,
             "impressions": 0, "clicks": 0, "ctr": 0.0, "avg_cpc": 0.0, "conversions": 0.0,
             "cost_per_conversion": 0.0, "conversions_value": 0.0},
        ]),
    }
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts: ({"Acme Co": {"status": "Live and spending", "recent_work": "Fixed the landing page"}}, []),
    )

    records, batch_results = mod.build_atlas_report()

    assert len(records) == 1
    record = records[0]
    assert record["atlas_id"] == "acme-123"
    assert record["company_name"] == "Acme Co"
    assert record["is_live"] is True
    assert record["status"] == "Live and spending"
    assert record["recent_work"] == "Fixed the landing page"
    # Compressed: only the enabled campaign survives, not the removed one.
    assert len(record["google_ads"]["enabled_campaigns"]) == 1
    assert record["google_ads"]["enabled_campaigns"][0]["name"] == "Active"
    assert record["google_ads"]["total_cost"] == 50.0


def test_account_with_no_google_mcc_id_gets_a_null_google_ads_field_not_an_error(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("No CID Co", google_ads_customer_id=None)]
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts: ({}, []))

    records, _ = mod.build_atlas_report()

    assert records[0]["google_ads"] is None
    assert records[0]["google_ads_error"] is None
    assert records[0]["is_live"] is True  # falls back to Atlas's isActive


def test_bad_customer_id_is_soft_failed_not_run_crashing(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Bad CID Co", google_ads_customer_id="9999999999")]
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts: ({}, []))

    records, _ = mod.build_atlas_report()

    assert records[0]["google_ads"] is None
    assert "no fake response" in records[0]["google_ads_error"]


def test_limit_caps_the_universe_sorted_by_company_name(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Charlie"), _atlas_account("Alpha"), _atlas_account("Beta")]
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts: ({}, []))

    records, _ = mod.build_atlas_report(limit=2)

    assert [r["company_name"] for r in records] == ["Alpha", "Beta"]
