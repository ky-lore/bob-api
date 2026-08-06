"""
Tests the adspend FastAPI routes directly against faked GoogleAdsClient/
AtlasClient classes -- proves the two entry points (direct customer_id, and
Atlas-resolved) actually wire together, not just that each client works
alone.
"""
import pytest
from fastapi.testclient import TestClient

import adspend.main as mod
from adspend.config import get_settings


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_SECRET", "x")
    monkeypatch.setenv("GOOGLE_ADS_REFRESH_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "3910981944")
    monkeypatch.setenv("META_ACCESS_TOKEN", "x")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()


class _FakeGoogleAdsClient:
    def get_account_spend(self, customer_id, date_range="YESTERDAY"):
        return {
            "customer_id": customer_id,
            "date_range": date_range,
            "total_cost": 42.5,
            "enabled_campaign_count": 2,
            "campaigns": [],
        }


class _FakeMetaAdsClient:
    def get_account_spend(self, ad_account_id, date_range="YESTERDAY"):
        return {
            "ad_account_id": ad_account_id,
            "date_range": date_range,
            "total_cost": 17.0,
            "enabled_campaign_count": 1,
            "campaigns": [],
        }


class _FakeAtlasClient:
    def get_all_accounts(self):
        return [{
            "id": "acme-co",
            "companyName": "Acme Co",
            "integrations": {"googleMccId": "1234567890", "metaAdAccountId": "act_9876543210"},
        }]


def test_spend_by_customer_id(monkeypatch):
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    client = TestClient(mod.app)

    resp = client.get("/accounts/1234567890/spend")

    assert resp.status_code == 200
    assert resp.json()["total_cost"] == 42.5


def test_spend_by_atlas_id_resolves_google_mcc_id_and_merges_metadata(monkeypatch):
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    client = TestClient(mod.app)

    resp = client.get("/atlas-accounts/acme-co/spend")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cost"] == 42.5
    assert body["company_name"] == "Acme Co"
    assert body["customer_id"] == "1234567890"


def test_spend_by_atlas_id_404s_when_atlas_id_unknown(monkeypatch):
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    client = TestClient(mod.app)

    resp = client.get("/atlas-accounts/nonexistent/spend")

    assert resp.status_code == 404


def test_invalid_date_range_is_rejected_with_400():
    client = TestClient(mod.app)

    resp = client.get("/accounts/1234567890/spend", params={"date_range": "LAST_QUARTER"})

    assert resp.status_code == 400


def test_meta_spend_by_ad_account_id(monkeypatch):
    monkeypatch.setattr(mod, "MetaAdsClient", _FakeMetaAdsClient)
    client = TestClient(mod.app)

    resp = client.get("/accounts/act_9876543210/meta-spend")

    assert resp.status_code == 200
    assert resp.json()["total_cost"] == 17.0


def test_meta_spend_by_atlas_id_resolves_meta_ad_account_id_and_merges_metadata(monkeypatch):
    monkeypatch.setattr(mod, "MetaAdsClient", _FakeMetaAdsClient)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    client = TestClient(mod.app)

    resp = client.get("/atlas-accounts/acme-co/meta-spend")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cost"] == 17.0
    assert body["company_name"] == "Acme Co"
    assert body["ad_account_id"] == "act_9876543210"


def test_meta_spend_by_atlas_id_404s_when_no_meta_ad_account_id(monkeypatch):
    class _NoMetaAtlasClient:
        def get_all_accounts(self):
            return [{"id": "acme-co", "companyName": "Acme Co", "integrations": {}}]

    monkeypatch.setattr(mod, "AtlasClient", _NoMetaAtlasClient)
    client = TestClient(mod.app)

    resp = client.get("/atlas-accounts/acme-co/meta-spend")

    assert resp.status_code == 404


def test_meta_invalid_date_range_is_rejected_with_400():
    client = TestClient(mod.app)

    resp = client.get("/accounts/act_123/meta-spend", params={"date_range": "LAST_QUARTER"})

    assert resp.status_code == 400
