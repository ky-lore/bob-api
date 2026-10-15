"""
Tests the /reports/sales-audit routes: page render (with the data island
safely embedded), override upsert/clear + the render-time overlay, the
password gate, and the async trigger/poll pair. Same isolated-app +
temp-file-SQLite convention as test_atlas_report_router.py.
"""
import json
import time
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routers.sales_audit as router_mod
from app.db import Base, get_db
from app.models import SalesAuditRun, SalesDealOverride

PW = {"X-Admin-Password": router_mod._ADMIN_OVERRIDE_PASSWORD}


def _row(key, client="Acme Roofing", owner="Needs rep", typ="Package", **kw):
    return {"key": key, "type": typ, "client": client, "contact": "Al", "close_date": "2026-07-04", "close_month": "2026-07",
            "close_date_basis": "atlas created", "amount": 2500.0, "amount_basis": "Atlas billing", "owner": owner,
            "owner_basis": "none", "source_type": "Unknown", "source_detail": None, "event": None, "referral_partner": None,
            "tags": [], "where": None, "flags": ["needs_rep"] if owner == "Needs rep" else [], "call_score": {}, "zoom_detail": [],
            "first_seen": "2026-10-15", **kw}


def _setup(tmp_path, rows=None, source_errors=None):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(bind=engine)
    sf = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_db] = lambda: sf()
    if rows is not None:
        with sf() as db:
            db.add(SalesAuditRun(run_at=datetime(2026, 10, 15, 12), report_json=json.dumps(
                {"generated_at": "2026-10-15T12:00:00+00:00", "count": len(rows), "rows": rows, "summary": {},
                 "source_errors": source_errors or {}, "rules": {}})))
            db.commit()
    return TestClient(app), sf


def test_page_renders_empty_state_with_no_runs(tmp_path):
    client, _ = _setup(tmp_path)
    r = client.get("/reports/sales-audit")
    assert r.status_code == 200 and "No audit has run yet" in r.text


def test_page_embeds_rows_as_json_and_escapes_script_breakouts(tmp_path):
    evil = "</script><script>alert(1)</script>"
    client, _ = _setup(tmp_path, rows=[_row("atlas:1", client=evil)])
    r = client.get("/reports/sales-audit")
    assert r.status_code == 200
    assert "</script><script>alert(1)" not in r.text                # tojson must escape the breakout
    assert 'id="sales-data"' in r.text and "Acme" not in r.text.split('id="sales-data"')[0]


def test_source_error_banner_is_shown(tmp_path):
    client, _ = _setup(tmp_path, rows=[_row("atlas:1")], source_errors={"billing_sheet": "PermissionError: nope"})
    assert "Some sources failed" in client.get("/reports/sales-audit").text


def test_latest_json_applies_overrides_and_keeps_the_pipeline_value(tmp_path):
    client, _ = _setup(tmp_path, rows=[_row("atlas:1"), _row("atlas:2", client="Beta")])
    ok = client.post("/reports/sales-audit/overrides", headers=PW,
                     json={"deal_key": "atlas:1", "client_name": "Acme Roofing", "owner": "Kyle Kellner", "note": "per Bob"})
    assert ok.status_code == 200
    rows = {r["key"]: r for r in client.get("/reports/sales-audit/latest").json()["rows"]}
    a, b = rows["atlas:1"], rows["atlas:2"]
    assert a["owner"] == "Kyle Kellner" and a["owner_basis"] == "manual override" and a["auto_owner"] == "Needs rep"
    assert "needs_rep" not in a["flags"] and a["overridden"] is True
    assert b["owner"] == "Needs rep" and b["overridden"] is False


def test_type_move_and_exclusion_overlay(tmp_path):
    client, _ = _setup(tmp_path, rows=[_row("atlas:1")])
    client.post("/reports/sales-audit/overrides", headers=PW,
                json={"deal_key": "atlas:1", "client_name": "x", "deal_type": "Digital Diagnosis", "excluded": True})
    (row,) = client.get("/reports/sales-audit/latest").json()["rows"]
    assert row["type"] == "Digital Diagnosis" and row["auto_type"] == "Package" and row["excluded"] is True


def test_override_requires_password_and_valid_values(tmp_path):
    client, _ = _setup(tmp_path, rows=[_row("atlas:1")])
    body = {"deal_key": "atlas:1", "client_name": "x", "owner": "Kyle Kellner"}
    assert client.post("/reports/sales-audit/overrides", json=body).status_code == 401
    assert client.post("/reports/sales-audit/overrides", headers={"X-Admin-Password": "nope"}, json=body).status_code == 401
    assert client.post("/reports/sales-audit/overrides", headers=PW, json={**body, "owner": "Simon Ting"}).status_code == 422
    assert client.post("/reports/sales-audit/overrides", headers=PW, json={**body, "owner": None, "deal_type": "Bogus"}).status_code == 422
    assert client.delete("/reports/sales-audit/overrides/atlas:1").status_code == 401


def test_empty_override_clears_the_row_and_delete_works(tmp_path):
    client, sf = _setup(tmp_path, rows=[_row("atlas:1")])
    client.post("/reports/sales-audit/overrides", headers=PW, json={"deal_key": "atlas:1", "client_name": "x", "owner": "Paul Rastrelli"})
    with sf() as db:
        assert db.query(SalesDealOverride).count() == 1
    client.post("/reports/sales-audit/overrides", headers=PW, json={"deal_key": "atlas:1", "client_name": "x"})  # nothing set
    with sf() as db:
        assert db.query(SalesDealOverride).count() == 0
    client.post("/reports/sales-audit/overrides", headers=PW, json={"deal_key": "atlas:1", "client_name": "x", "excluded": True})
    r = client.delete("/reports/sales-audit/overrides/atlas:1", headers=PW)     # key contains ':'
    assert r.json()["cleared"] is True


def test_historic_view_and_run_id_param(tmp_path):
    client, _ = _setup(tmp_path, rows=[_row("atlas:1")])
    assert client.get("/reports/sales-audit/view/1").status_code == 200
    assert client.get("/reports/sales-audit/view/99").status_code == 404
    assert client.get("/reports/sales-audit/latest?run_id=1").json()["run_id"] == 1


def test_trigger_and_poll(monkeypatch, tmp_path):
    client, _ = _setup(tmp_path)

    class _Run:
        id, run_at = 5, datetime(2026, 10, 15, 12)

    monkeypatch.setattr(router_mod, "run_and_store_sales_audit", lambda db, on_progress=None: _Run())

    class _Db:
        def close(self):
            pass

    monkeypatch.setattr(router_mod, "get_session_factory", lambda: (lambda: _Db()))
    job = client.post("/reports/sales-audit/run").json()
    body = client.get(f"/reports/sales-audit/run/{job['job_id']}").json()
    deadline = time.time() + 5
    while body["job_status"] == "running" and time.time() < deadline:
        time.sleep(0.01)
        body = client.get(f"/reports/sales-audit/run/{job['job_id']}").json()
    assert body["job_status"] == "done" and body["run_id"] == 5
    assert client.get("/reports/sales-audit/run/nope").status_code == 404


# ---------------------------------------------------------------- push to Atlas

class _FakeAtlas:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def set_sales_attribution(self, atlas_id, *, actor, **kw):
        self.calls.append({"atlas_id": atlas_id, "actor": actor, **kw})
        if self.error:
            raise self.error
        return {"changed": list(kw), "salesRep": {"email": kw.get("sales_rep")}, "referralPartner": kw.get("referral_partner")}


def _push_setup(tmp_path, monkeypatch, atlas):
    client, sf = _setup(tmp_path, rows=[_row("atlas:1", atlas_id="A1"), _row("dd:9", client="No Atlas Co")])
    monkeypatch.setattr(router_mod, "_atlas_client", lambda: atlas)
    return client, sf


def _push(client, **kw):
    return client.post("/reports/sales-audit/push", headers=PW, json={"deal_key": "atlas:1", "set_by": "Bob", **kw})


def test_push_rep_writes_to_atlas_with_the_mapped_email_and_stamps_the_override(tmp_path, monkeypatch):
    atlas = _FakeAtlas()
    client, sf = _push_setup(tmp_path, monkeypatch, atlas)
    r = _push(client, owner="Kyle Kellner")
    assert r.status_code == 200 and r.json()["pushed"] is True
    assert atlas.calls == [{"atlas_id": "A1", "actor": "Bob", "sales_rep": "kylekellner@advancedmarketers.co"}]
    with sf() as db:
        o = db.query(SalesDealOverride).one()
        assert o.owner == "Kyle Kellner" and o.atlas_pushed_at is not None and o.set_by == "Bob"
    (row,) = [x for x in client.get("/reports/sales-audit/latest").json()["rows"] if x["key"] == "atlas:1"]
    assert row["owner"] == "Kyle Kellner" and row["atlas_pushed_at"] and "needs_rep" not in row["flags"]


def test_push_referral_partner_updates_the_row_immediately(tmp_path, monkeypatch):
    atlas = _FakeAtlas()
    client, _ = _push_setup(tmp_path, monkeypatch, atlas)
    assert _push(client, referral_partner="  Acme Partners ").json()["pushed"] is True
    assert atlas.calls[0]["referral_partner"] == "Acme Partners"          # trimmed
    (row,) = [x for x in client.get("/reports/sales-audit/latest").json()["rows"] if x["key"] == "atlas:1"]
    assert row["source_type"] == "Referral" and row["referral_partner"] == "Acme Partners" and "written to Atlas" in row["source_detail"]


def test_atlas_rejection_keeps_the_local_override_unpushed_and_reports_why(tmp_path, monkeypatch):
    from app.integrations.atlas_client import AtlasWriteError
    atlas = _FakeAtlas(error=AtlasWriteError(404, "Not found"))
    client, sf = _push_setup(tmp_path, monkeypatch, atlas)
    body = _push(client, owner="Paul Rastrelli").json()
    assert body["pushed"] is False and body["saved_locally"] is True and "may not be deployed" in body["error"]
    with sf() as db:
        o = db.query(SalesDealOverride).one()
        assert o.owner == "Paul Rastrelli" and o.atlas_pushed_at is None      # intent kept, not marked synced


def test_network_failure_is_also_saved_locally(tmp_path, monkeypatch):
    client, sf = _push_setup(tmp_path, monkeypatch, _FakeAtlas(error=TimeoutError("slow")))
    body = _push(client, owner="Paul Rastrelli").json()
    assert body["pushed"] is False and "TimeoutError" in body["error"]
    with sf() as db:
        assert db.query(SalesDealOverride).one().owner == "Paul Rastrelli"


def test_push_validation_and_guards(tmp_path, monkeypatch):
    atlas = _FakeAtlas()
    client, sf = _push_setup(tmp_path, monkeypatch, atlas)
    assert client.post("/reports/sales-audit/push", json={"deal_key": "atlas:1", "owner": "Kyle Kellner", "set_by": "B"}).status_code == 401
    assert _push(client).status_code == 422                                              # nothing to push
    assert _push(client, owner="Simon Ting").status_code == 422                          # not a counted rep
    assert _push(client, referral_partner="   ").status_code == 422                      # blank partner
    assert client.post("/reports/sales-audit/push", headers=PW, json={"deal_key": "nope", "owner": "Kyle Kellner", "set_by": "B"}).status_code == 404
    r = client.post("/reports/sales-audit/push", headers=PW, json={"deal_key": "dd:9", "owner": "Kyle Kellner", "set_by": "B"})
    assert r.status_code == 422 and "no Atlas account" in r.json()["detail"]             # row has no atlas_id
    assert atlas.calls == []                                                             # none of the above reached Atlas
    with sf() as db:
        assert db.query(SalesDealOverride).count() == 0                                  # and none left an override behind


def test_page_embeds_the_atlas_web_base_without_a_trailing_slash(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("ATLAS_WEB_BASE_URL", "https://atlas.example.test/")
    get_settings.cache_clear()
    try:
        client, _ = _setup(tmp_path, rows=[_row("atlas:1", atlas_id="A1")])
        html = client.get("/reports/sales-audit").text
    finally:
        get_settings.cache_clear()
    assert 'var ATLAS_WEB = "https://atlas.example.test";' in html


def test_atlas_web_base_defaults_to_empty_so_links_stay_hidden(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.delenv("ATLAS_WEB_BASE_URL", raising=False)
    get_settings.cache_clear()
    try:
        client, _ = _setup(tmp_path, rows=[_row("atlas:1", atlas_id="A1")])
        html = client.get("/reports/sales-audit").text
    finally:
        get_settings.cache_clear()
    assert 'var ATLAS_WEB = "";' in html


def test_push_also_applies_the_audit_only_fields_from_the_same_form(tmp_path, monkeypatch):
    atlas = _FakeAtlas()
    client, sf = _push_setup(tmp_path, monkeypatch, atlas)
    r = _push(client, owner="Kyle Kellner", excluded=True, note="dup of another deal", deal_type="Digital Diagnosis")
    assert r.json()["pushed"] is True
    assert atlas.calls == [{"atlas_id": "A1", "actor": "Bob", "sales_rep": "kylekellner@advancedmarketers.co"}]   # audit-only fields never go to Atlas
    with sf() as db:
        o = db.query(SalesDealOverride).one()
        assert (o.owner, o.excluded, o.note, o.deal_type) == ("Kyle Kellner", True, "dup of another deal", "Digital Diagnosis")


def test_clearing_the_rep_does_not_wipe_a_referral_partner_override(tmp_path, monkeypatch):
    client, sf = _push_setup(tmp_path, monkeypatch, _FakeAtlas())
    _push(client, referral_partner="Acme Partners")
    client.post("/reports/sales-audit/overrides", headers=PW, json={"deal_key": "atlas:1", "client_name": "x"})   # "nothing set" save
    with sf() as db:
        assert db.query(SalesDealOverride).one().referral_partner == "Acme Partners"
