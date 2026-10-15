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


REPS = [{"id": "1", "name": "Kyle Kellner", "email": "kylekellner@advancedmarketers.co"},
        {"id": "2", "name": "Chris paniagua", "email": "chris@advancedmarketers.co"},
        {"id": "3", "name": "Anthony Aguilar", "email": "anthonyaguilar@advancedmarketers.co"}]


class _Atlas:
    def __init__(self, reps=REPS, fail=False):
        self._reps, self._fail = reps, fail

    def get_sales_reps(self):
        if self._fail:
            raise RuntimeError("atlas roster down")
        return self._reps

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

    def list_user_details(self):
        return [{"id": "g-kyle", "name": "Kyle K", "email": "kylekellner@advancedmarketers.co"}]

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


# ------------------------------------------------ roster: pulled from Atlas every run

def _run(db, **kw):
    kw.setdefault("atlas", _Atlas()); kw.setdefault("ghl", _Ghl()); kw.setdefault("drive", _BadDrive()); kw.setdefault("zoom", ZoomIndex([]))
    return json.loads(mod.run_and_store_sales_audit(db, **kw).report_json)


def test_the_run_stores_the_atlas_roster_tidied_and_lists_it_in_the_rules(db):
    p = _run(db)
    assert [r["name"] for r in p["roster"]] == ["Kyle Kellner", "Chris Paniagua", "Anthony Aguilar"]          # Atlas's "Chris paniagua" tidied
    assert p["roster"][2] == {"name": "Anthony Aguilar", "email": "anthonyaguilar@advancedmarketers.co"}
    assert p["rules"]["reps"] == ["Kyle Kellner", "Chris Paniagua", "Anthony Aguilar"] and "atlas_roster" not in p["source_errors"]


def test_roster_failure_falls_back_to_the_last_stored_roster_and_says_so(db):
    _run(db)                                                                                                    # stores a roster incl. Anthony
    p = _run(db, atlas=_Atlas(fail=True))
    assert [r["name"] for r in p["roster"]] == ["Kyle Kellner", "Chris Paniagua", "Anthony Aguilar"]
    assert "atlas_roster" in p["source_errors"] and "last known roster" in p["source_errors"]["atlas_roster"]
    assert p["rows"][0]["owner"] == "Kyle Kellner"                                                              # attribution still works


def test_with_no_stored_roster_a_failure_uses_the_legacy_four_and_still_reports_it(db):
    p = _run(db, atlas=_Atlas(fail=True))
    assert [r["name"] for r in p["roster"]] == [r["name"] for r in cfg.LEGACY_ROSTER] and "atlas_roster" in p["source_errors"]


def test_an_empty_roster_is_treated_as_a_failure_not_as_nobody_counts(db):
    p = _run(db, atlas=_Atlas(reps=[]))
    assert len(p["roster"]) == len(cfg.LEGACY_ROSTER) and "empty" in p["source_errors"]["atlas_roster"]
    assert p["rows"][0]["owner"] == "Kyle Kellner"                                                              # not silently "Needs rep"


def test_zoom_index_only_includes_roster_hosts_and_names_them_from_the_roster(db):
    from datetime import datetime
    from app.models import ZoomCallRecord
    from app.tasks.sales_audit_blend import Roster
    mk = lambda uuid, host: ZoomCallRecord(meeting_uuid=uuid, host_email=host, topic="Acme x AM", start_time=datetime(2026, 7, 8, 17),
                                           transcript_text="00:00:01.000 --> 00:30:00.000\nhi", pulled_at=datetime(2026, 7, 9))
    db.add_all([mk("a", "AnthonyAguilar@advancedmarketers.co"), mk("b", "kylekellner@advancedmarketers.co"), mk("c", "stranger@advancedmarketers.co")])
    db.commit()
    idx = ZoomIndex.from_db(db, Roster.from_atlas(REPS))
    assert sorted(e["rep"] for e in idx.entries) == ["Anthony Aguilar", "Kyle Kellner"] and all(e["min"] == 30 for e in idx.entries)
    assert ZoomIndex.from_db(db, Roster([])).entries == []
