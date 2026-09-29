"""
Tests the /reports/cmdctr routes -- trigger/poll/latest, same shape as
test_atlas_report_router.py's trio for the full-universe run. Isolated
FastAPI app (just this router) with get_db overridden to a real temp-file
SQLite session, same convention as that file -- not app.main, which starts
a real BackgroundScheduler singleton unsafe to start twice per process.
"""
import json
import time
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routers.cmdctr_report as router_mod
from app.db import Base, get_db
from app.models import CmdctrRun


class _FakeDB:
    """run_and_push_cmdctr_report itself is mocked in the trigger/poll tests
    below -- the background thread just needs a db-shaped object that
    survives get_session_factory()() and .close()."""

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
    body = client.get(f"/reports/cmdctr/run/{job_id}").json()
    while body["job_status"] == "running":
        if time.time() > deadline:
            raise TimeoutError(f"job {job_id} still running after {timeout}s")
        time.sleep(0.01)
        body = client.get(f"/reports/cmdctr/run/{job_id}").json()
    return body


class _FakeRun:
    def __init__(self, run_id, report_json):
        self.id = run_id
        self.run_at = datetime(2026, 9, 29, 20, 0, 0)
        self.report_json = report_json


def test_trigger_starts_a_background_job_and_returns_its_result_on_poll(monkeypatch, tmp_path):
    def _fake_run_and_push(db, on_progress=None):
        return _FakeRun(3, json.dumps({
            "count": 1, "accounts": [{"company_name": "Onboarding Co"}],
            "narrative_batches": [], "pulse_push": {"ok": True, "error": None},
        }))

    monkeypatch.setattr(router_mod, "run_and_push_cmdctr_report", _fake_run_and_push)
    monkeypatch.setattr(router_mod, "get_session_factory", lambda: (lambda: _FakeDB()))
    client = _client(tmp_path)

    trigger_response = client.post("/reports/cmdctr/run").json()
    assert trigger_response["job_status"] == "running"

    body = _wait_for_job(client, trigger_response["job_id"])

    assert body["job_status"] == "done"
    assert body["run_id"] == 3
    assert body["count"] == 1
    assert body["accounts"][0]["company_name"] == "Onboarding Co"
    assert body["pulse_push"] == {"ok": True, "error": None}


def test_trigger_job_error_surfaces_on_poll(monkeypatch, tmp_path):
    def _always_fail(db, on_progress=None):
        raise RuntimeError("Atlas pull failed")

    monkeypatch.setattr(router_mod, "run_and_push_cmdctr_report", _always_fail)
    monkeypatch.setattr(router_mod, "get_session_factory", lambda: (lambda: _FakeDB()))
    client = _client(tmp_path)

    trigger_response = client.post("/reports/cmdctr/run").json()
    body = _wait_for_job(client, trigger_response["job_id"])

    assert body["job_status"] == "error"
    assert "Atlas pull failed" in body["error"]


def test_unknown_job_id_returns_404(tmp_path):
    resp = _client(tmp_path).get("/reports/cmdctr/run/nonexistent")
    assert resp.status_code == 404


def test_latest_reads_the_most_recently_persisted_run_directly_from_the_db(tmp_path):
    client, session_factory = _client_and_session_factory(tmp_path)
    db = session_factory()
    db.add(CmdctrRun(
        run_at=datetime(2026, 9, 29, 19, 0, 0),
        report_json=json.dumps({"count": 1, "accounts": [{"company_name": "Older Run Co"}], "narrative_batches": []}),
    ))
    db.add(CmdctrRun(
        run_at=datetime(2026, 9, 29, 20, 0, 0),
        report_json=json.dumps({"count": 1, "accounts": [{"company_name": "Latest Run Co"}], "narrative_batches": []}),
    ))
    db.commit()
    db.close()

    resp = client.get("/reports/cmdctr/latest")

    assert resp.status_code == 200
    body = resp.json()
    assert body["accounts"][0]["company_name"] == "Latest Run Co"


def test_latest_with_no_runs_yet_returns_an_empty_shape_not_an_error(tmp_path):
    resp = _client(tmp_path).get("/reports/cmdctr/latest")

    assert resp.status_code == 200
    assert resp.json() == {"run_id": None, "run_at": None, "count": 0, "accounts": [], "narrative_batches": []}
