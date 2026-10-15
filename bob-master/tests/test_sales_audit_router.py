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
