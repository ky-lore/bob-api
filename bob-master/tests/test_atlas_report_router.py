"""
Tests the /reports/atlas-account-status routes -- the original synchronous
GET, plus the async trigger/poll/latest trio added 2026-09-18 for the full,
unbounded account universe (a real unlimited pull 502's past Railway's
~300s gateway timeout -- see the router's module docstring). Uses an
isolated FastAPI app (just this router) with get_db overridden to a real
temp-file SQLite session, same convention as test_dashboard_route.py --
not app.main, which starts a real BackgroundScheduler singleton unsafe to
start twice per process (see test_trigger_endpoint.py), and whose real
Depends(get_db) would otherwise reach for a real DATABASE_URL.
"""
import json
import time
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routers.atlas_report as router_mod
from app.db import Base, get_db
from app.models import AtlasReportRun


class _FakeDB:
    """run_and_store_atlas_report itself is mocked in the trigger/poll tests
    below -- the background thread just needs a db-shaped object that
    survives get_session_factory()() and .close(), same convention as
    test_zoom_call_sync_router.py's _FakeDB."""

    def close(self):
        pass


def _client_and_session_factory(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_db] = lambda: session_factory()
    return TestClient(app), session_factory


def _client(tmp_path):
    client, _ = _client_and_session_factory(tmp_path)
    return client


def _wait_for_job(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    body = client.get(f"/reports/atlas-account-status/run/{job_id}").json()
    while body["job_status"] == "running":
        if time.time() > deadline:
            raise TimeoutError(f"job {job_id} still running after {timeout}s")
        time.sleep(0.01)
        body = client.get(f"/reports/atlas-account-status/run/{job_id}").json()
    return body


def test_endpoint_is_mounted_and_returns_the_report_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(
        router_mod,
        "build_atlas_report",
        lambda db=None, limit=None: (
            [{"atlas_id": "acme-123", "company_name": "Acme Co", "health": "on_track", "status": "ok", "recent_work": "did stuff"}],
            [{"batch_index": 0, "ok": True}],
        ),
    )

    resp = _client(tmp_path).get("/reports/atlas-account-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["accounts"][0]["atlas_id"] == "acme-123"
    assert body["accounts"][0]["health"] == "on_track"
    assert body["narrative_batches"][0]["ok"] is True


def test_limit_query_param_is_passed_through(monkeypatch, tmp_path):
    captured = {}

    def _fake(db=None, limit=None):
        captured["limit"] = limit
        captured["db_passed"] = db is not None
        return [], []

    monkeypatch.setattr(router_mod, "build_atlas_report", _fake)

    _client(tmp_path).get("/reports/atlas-account-status", params={"limit": 5})

    assert captured["limit"] == 5
    assert captured["db_passed"] is True


class _FakeRun:
    def __init__(self, run_id, report_json):
        self.id = run_id
        self.run_at = datetime(2026, 9, 18, 12, 0, 0)
        self.report_json = report_json


def test_trigger_starts_a_background_job_and_returns_its_result_on_poll(monkeypatch, tmp_path):
    captured = {}

    def _fake_run_and_store(db, limit=None):
        captured["limit"] = limit
        return _FakeRun(7, json.dumps({"count": 1, "accounts": [{"company_name": "Acme Co"}], "narrative_batches": []}))

    monkeypatch.setattr(router_mod, "run_and_store_atlas_report", _fake_run_and_store)
    monkeypatch.setattr(router_mod, "get_session_factory", lambda: (lambda: _FakeDB()))
    client = _client(tmp_path)

    trigger_response = client.post("/reports/atlas-account-status/run", params={"limit": 20}).json()
    assert trigger_response["job_status"] == "running"

    body = _wait_for_job(client, trigger_response["job_id"])

    assert captured["limit"] == 20
    assert body["job_status"] == "done"
    assert body["run_id"] == 7
    assert body["count"] == 1
    assert body["accounts"][0]["company_name"] == "Acme Co"


def test_trigger_job_error_surfaces_on_poll(monkeypatch, tmp_path):
    def _always_fail(db, limit=None):
        raise RuntimeError("Atlas pull failed")

    monkeypatch.setattr(router_mod, "run_and_store_atlas_report", _always_fail)
    monkeypatch.setattr(router_mod, "get_session_factory", lambda: (lambda: _FakeDB()))
    client = _client(tmp_path)

    trigger_response = client.post("/reports/atlas-account-status/run").json()
    body = _wait_for_job(client, trigger_response["job_id"])

    assert body["job_status"] == "error"
    assert "Atlas pull failed" in body["error"]


def test_unknown_job_id_returns_404(tmp_path):
    resp = _client(tmp_path).get("/reports/atlas-account-status/run/nonexistent")
    assert resp.status_code == 404


def test_latest_reads_the_most_recently_persisted_run_directly_from_the_db(tmp_path):
    client, session_factory = _client_and_session_factory(tmp_path)
    db = session_factory()
    db.add(AtlasReportRun(
        run_at=datetime(2026, 9, 17, 9, 0, 0),
        limit_used=None,
        report_json=json.dumps({"count": 1, "accounts": [{"company_name": "Older Run Co"}], "narrative_batches": []}),
    ))
    db.add(AtlasReportRun(
        run_at=datetime(2026, 9, 18, 9, 0, 0),
        limit_used=None,
        report_json=json.dumps({"count": 1, "accounts": [{"company_name": "Latest Run Co"}], "narrative_batches": []}),
    ))
    db.commit()
    db.close()

    resp = client.get("/reports/atlas-account-status/latest")

    assert resp.status_code == 200
    body = resp.json()
    assert body["accounts"][0]["company_name"] == "Latest Run Co"


def test_latest_with_no_runs_yet_returns_an_empty_shape_not_an_error(tmp_path):
    resp = _client(tmp_path).get("/reports/atlas-account-status/latest")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"run_id": None, "run_at": None, "count": 0, "accounts": [], "narrative_batches": []}
