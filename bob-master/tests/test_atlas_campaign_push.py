"""
Tests app.tasks.atlas_campaign_push.push_campaign_spend with every external
client faked -- proves one POST per (account, platform) that has an ID on
file, the dry_run no-op-on-Atlas path, soft-fail behavior for both a bad
spend pull and a failed Atlas POST, and the "compressed" campaign list
(enabled + active-in-window only, matching atlas_report.py's Google-only
version but now for both platforms).
"""
from datetime import datetime, timedelta, timezone

import app.tasks.atlas_campaign_push as mod


def _atlas_account(company_name, *, atlas_id=None, google_ads_customer_id=None, meta_ad_account_id=None):
    return {
        "id": atlas_id or company_name.lower().replace(" ", "-"),
        "companyName": company_name,
        "isActive": True,
        "integrations": {
            "googleMccId": google_ads_customer_id,
            "metaAdAccountId": meta_ad_account_id,
        },
    }


class _FakeAtlasClient:
    accounts: list = []
    posted: list = []
    fail_post_for_atlas_id: str | None = None

    def get_all_accounts(self):
        return _FakeAtlasClient.accounts

    def post_campaigns(self, atlas_id, payload):
        if atlas_id == _FakeAtlasClient.fail_post_for_atlas_id:
            raise RuntimeError("Atlas 500")
        _FakeAtlasClient.posted.append((atlas_id, payload))
        return {"ok": True}


class _FakeGoogleAdsClient:
    responses: dict = {}

    def get_account_spend(self, customer_id, date_range="LAST_30_DAYS"):
        if customer_id not in _FakeGoogleAdsClient.responses:
            raise RuntimeError(f"no fake google response for {customer_id}")
        return _FakeGoogleAdsClient.responses[customer_id]


class _FakeMetaAdsClient:
    responses: dict = {}

    def get_account_spend(self, ad_account_id, date_range="LAST_30_DAYS"):
        if ad_account_id not in _FakeMetaAdsClient.responses:
            raise RuntimeError(f"no fake meta response for {ad_account_id}")
        return _FakeMetaAdsClient.responses[ad_account_id]


def _spend(total_cost=0.0, campaigns=None):
    campaigns = campaigns or []
    return {
        "total_cost": total_cost,
        "total_impressions": 100,
        "total_clicks": 5,
        "total_conversions": 1.0,
        "campaigns": campaigns,
    }


def _campaign(status="ENABLED", cost=10.0, impressions=100, clicks=5, name="Search"):
    return {
        "id": "c1", "name": name, "status": status, "cost": cost,
        "impressions": impressions, "clicks": clicks, "conversions": 1.0,
    }


def _setup(monkeypatch):
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    monkeypatch.setattr(mod, "MetaAdsClient", _FakeMetaAdsClient)
    _FakeAtlasClient.accounts = []
    _FakeAtlasClient.posted = []
    _FakeAtlasClient.fail_post_for_atlas_id = None
    _FakeGoogleAdsClient.responses = {}
    _FakeMetaAdsClient.responses = {}


def test_dry_run_builds_payloads_without_posting_to_atlas(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Acme Co", atlas_id="acme-1", google_ads_customer_id="1234567890")]
    _FakeGoogleAdsClient.responses = {"1234567890": _spend(50.0, [_campaign()])}

    results = mod.push_campaign_spend(dry_run=True)

    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["payload"]["platform"] == "google"
    assert _FakeAtlasClient.posted == []  # never actually posted


def test_real_run_posts_one_call_per_platform_with_an_id_on_file(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account(
        "Acme Co", atlas_id="acme-1", google_ads_customer_id="1234567890", meta_ad_account_id="act_555",
    )]
    _FakeGoogleAdsClient.responses = {"1234567890": _spend(50.0, [_campaign(name="Google Search")])}
    _FakeMetaAdsClient.responses = {"act_555": _spend(30.0, [_campaign(name="Meta Feed")])}

    results = mod.push_campaign_spend(dry_run=False)

    assert len(results) == 2
    assert {r["platform"] for r in results} == {"google", "meta"}
    assert all(r["ok"] for r in results)
    assert len(_FakeAtlasClient.posted) == 2
    posted_by_platform = {p["platform"]: (atlas_id, p) for atlas_id, p in _FakeAtlasClient.posted}
    assert posted_by_platform["google"][0] == "acme-1"
    assert posted_by_platform["google"][1]["adAccountId"] == "1234567890"
    assert posted_by_platform["google"][1]["source"] == "google-ads-sync"
    assert posted_by_platform["meta"][1]["adAccountId"] == "act_555"
    assert posted_by_platform["meta"][1]["source"] == "meta-ads-sync"


def test_skips_a_platform_entirely_when_no_id_is_on_file(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Google Only Co", atlas_id="g-1", google_ads_customer_id="1234567890")]
    _FakeGoogleAdsClient.responses = {"1234567890": _spend(50.0, [_campaign()])}

    results = mod.push_campaign_spend(dry_run=True)

    assert len(results) == 1
    assert results[0]["platform"] == "google"


def test_spend_pull_failure_is_soft_failed_per_platform(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Bad CID Co", atlas_id="bad-1", google_ads_customer_id="9999999999")]
    # No fake response registered for 9999999999 -> raises inside the fake client.

    results = mod.push_campaign_spend(dry_run=True)

    assert len(results) == 1
    assert results[0]["ok"] is False
    assert "spend pull failed" in results[0]["error"]
    assert results[0]["payload"] is None


def test_atlas_post_failure_is_soft_failed_not_run_crashing(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [
        _atlas_account("Fails Co", atlas_id="fail-1", google_ads_customer_id="1111111111"),
        _atlas_account("Works Co", atlas_id="works-1", google_ads_customer_id="2222222222"),
    ]
    _FakeGoogleAdsClient.responses = {
        "1111111111": _spend(10.0, [_campaign()]),
        "2222222222": _spend(20.0, [_campaign()]),
    }
    _FakeAtlasClient.fail_post_for_atlas_id = "fail-1"

    results = mod.push_campaign_spend(dry_run=False)

    by_atlas_id = {r["atlas_id"]: r for r in results}
    assert by_atlas_id["fail-1"]["ok"] is False
    assert "Atlas POST failed" in by_atlas_id["fail-1"]["error"]
    assert by_atlas_id["fail-1"]["payload"] is not None  # payload was built even though the POST failed
    assert by_atlas_id["works-1"]["ok"] is True  # one account's failure doesn't block another's


def test_campaign_list_is_compressed_to_enabled_or_active_only(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Acme Co", atlas_id="acme-1", google_ads_customer_id="1234567890")]
    _FakeGoogleAdsClient.responses = {"1234567890": _spend(50.0, [
        _campaign(status="ENABLED", name="Active"),
        _campaign(status="REMOVED", cost=0.0, impressions=0, clicks=0, name="Dead"),
    ])}

    results = mod.push_campaign_spend(dry_run=True)

    campaigns = results[0]["payload"]["campaigns"]
    assert len(campaigns) == 1
    assert campaigns[0]["name"] == "Active"


def test_limit_caps_the_universe_sorted_by_company_name(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [
        _atlas_account("Charlie", google_ads_customer_id="3"),
        _atlas_account("Alpha", google_ads_customer_id="1"),
        _atlas_account("Beta", google_ads_customer_id="2"),
    ]
    _FakeGoogleAdsClient.responses = {
        "1": _spend(1.0, []), "2": _spend(2.0, []), "3": _spend(3.0, []),
    }

    results = mod.push_campaign_spend(limit=2, dry_run=True)

    assert [r["company_name"] for r in results] == ["Alpha", "Beta"]


def test_period_bounds_are_a_30_day_window_ending_yesterday():
    today = datetime.now(timezone.utc).date()
    start, end = mod._period_bounds()

    assert end == (today - timedelta(days=1)).isoformat()
    assert start == (today - timedelta(days=30)).isoformat()
