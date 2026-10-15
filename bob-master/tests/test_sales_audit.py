"""End-to-end for build_sales_audit / run_and_store_sales_audit with fake
Atlas/GHL/Drive clients and a real temp-file SQLite DB."""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import SalesAuditRun
from app.tasks import sales_audit as mod
from app.tasks import sales_audit_config as cfg
from app.tasks.sales_audit_sources import ZoomIndex


class _Atlas:
    def get_all_accounts(self):
        return [{"id": "a1", "companyName": "Acme Roofing", "createdAt": "2026-07-10T20:00:00Z", "stage": "onboarding", "users": [],
                 "salesRep": {"name": "Kyle Kellner"}, "billing": {"baseAmount": 3500, "introPricing": True}, "legacy": False,
                 "onboarding": {"completed": 9, "confirmed": 9, "total": 9}},
                {"id": "a2", "companyName": "Old Import", "createdAt": "2026-02-01T00:00:00Z", "stage": "live", "users": [], "legacy": True}]


class _Ghl:
    def __init__(self, fail=False):
        self.fail = fail

    def list_opportunities(self, pid):
        if self.fail:
            raise RuntimeError("ghl down")
        return []

    def list_pipeline_stages(self):
        return {}

    def list_users(self):
        return {}

    def get_contact(self, cid):
        return {"contact": {}}

    def contact_call_history(self, cid):
        return []

    def search_contact(self, q):
        return None


class _BadDrive:
    def tab_title_for_gid(self, *a):
        raise PermissionError("sheet not shared with service account")


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(bind=engine)
    with sessionmaker(bind=engine)() as session:
        yield session


def test_run_builds_rows_and_soft_fails_the_billing_sheet(db):
    run = mod.run_and_store_sales_audit(db, atlas=_Atlas(), ghl=_Ghl(), drive=_BadDrive(), zoom=ZoomIndex([]))
    payload = json.loads(run.report_json)
    assert payload["count"] == 1 and payload["rows"][0]["client"] == "Acme Roofing"
    assert payload["rows"][0]["amount"] == 2500 and payload["rows"][0]["owner"] == "Kyle Kellner"
    assert "PermissionError" in payload["source_errors"]["billing_sheet"] and "zoom" not in payload["source_errors"]
    assert payload["summary"]["package"] == {"count": 1, "amount": 2500, "needs_rep": 0}
    assert payload["rules"]["atlas_era_start"] == cfg.ATLAS_ERA_START


def test_ghl_failure_raises_and_stores_nothing(db):
    with pytest.raises(RuntimeError):
        mod.run_and_store_sales_audit(db, atlas=_Atlas(), ghl=_Ghl(fail=True), drive=_BadDrive(), zoom=ZoomIndex([]))
    assert db.query(SalesAuditRun).count() == 0


def test_first_seen_carries_forward_from_the_previous_snapshot(db):
    first = json.loads(mod.run_and_store_sales_audit(db, atlas=_Atlas(), ghl=_Ghl(), drive=_BadDrive(), zoom=ZoomIndex([])).report_json)
    previous = db.query(SalesAuditRun).one()
    stored = json.loads(previous.report_json)
    stored["rows"][0]["first_seen"] = "2026-10-01"                       # pretend it was first seen two weeks ago
    previous.report_json = json.dumps(stored)
    db.commit()
    second = json.loads(mod.run_and_store_sales_audit(db, atlas=_Atlas(), ghl=_Ghl(), drive=_BadDrive(), zoom=ZoomIndex([])).report_json)
    assert second["rows"][0]["first_seen"] == "2026-10-01"
    assert first["rows"][0]["first_seen"] != "2026-10-01"


# ------------------------------------------------ override reconcile (retire what Atlas now says)

def _o(key, **kw):
    from datetime import datetime
    from app.models import SalesDealOverride
    now = datetime(2026, 10, 15)
    return SalesDealOverride(deal_key=key, client_name="x", created_at=now, updated_at=now, **kw)


def test_reconcile_retires_owner_once_atlas_returns_it_and_keeps_the_rest(db):
    from datetime import datetime
    from app.models import SalesDealOverride
    db.add_all([
        _o("a", owner="Kyle Kellner", atlas_pushed_at=datetime(2026, 10, 15)),                       # Atlas now agrees -> gone entirely
        _o("b", owner="Kyle Kellner", atlas_pushed_at=datetime(2026, 10, 15)),                       # Atlas still says something else -> stays
        _o("c", owner="Kyle Kellner", excluded=True, atlas_pushed_at=datetime(2026, 10, 15)),        # agrees, but exclusion is audit-only -> keep row, drop owner
        _o("d", owner="Kyle Kellner"),                                                               # same rep but via GHL, not Atlas -> stays
    ])
    db.commit()
    rows = [{"key": "a", "owner": "Kyle Kellner", "owner_basis": "Atlas salesRep"},
            {"key": "b", "owner": "Paul Rastrelli", "owner_basis": "Atlas salesRep"},
            {"key": "c", "owner": "Kyle Kellner", "owner_basis": "Atlas salesRep"},
            {"key": "d", "owner": "Kyle Kellner", "owner_basis": "GHL opp assignee"}]
    assert mod.reconcile_overrides(db, rows) == 2
    left = {o.deal_key: o for o in db.query(SalesDealOverride).all()}
    assert set(left) == {"b", "c", "d"}
    assert left["b"].owner == "Kyle Kellner" and left["b"].atlas_pushed_at is not None
    assert left["c"].owner is None and left["c"].excluded is True and left["c"].atlas_pushed_at is None
    assert left["d"].owner == "Kyle Kellner"


def test_reconcile_retires_referral_partner_case_insensitively(db):
    from app.models import SalesDealOverride
    db.add_all([_o("a", referral_partner="acme partners"), _o("b", referral_partner="Acme Partners")])
    db.commit()
    rows = [{"key": "a", "owner": "x", "owner_basis": "x", "referral_partner": "Acme Partners", "source_detail": "Atlas referral partner: Acme Partners"},
            {"key": "b", "owner": "x", "owner_basis": "x", "referral_partner": "Acme Partners", "source_detail": "from GHL tags"}]   # not from Atlas -> stays
    assert mod.reconcile_overrides(db, rows) == 1
    assert [o.deal_key for o in db.query(SalesDealOverride).all()] == ["b"]


def test_a_run_retires_overrides_atlas_now_agrees_with_and_never_blocks_storing(db):
    from app.models import SalesDealOverride
    db.add(_o("atlas:a1", owner="Kyle Kellner"))
    db.commit()
    run = mod.run_and_store_sales_audit(db, atlas=_Atlas(), ghl=_Ghl(), drive=_BadDrive(), zoom=ZoomIndex([]))
    assert json.loads(run.report_json)["rows"][0]["owner"] == "Kyle Kellner"          # Atlas fixture's salesRep
    assert db.query(SalesDealOverride).count() == 0                                    # override retired by the run
