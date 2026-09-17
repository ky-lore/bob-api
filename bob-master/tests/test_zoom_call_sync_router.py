"""
Tests the /tasks/zoom-call-sync/run routes themselves -- proves they're
mounted on app.main and kick sync_zoom_calls off on job_tracker's background
thread, same job_id/poll shape as every other background task in this app
(see test_trigger_endpoint.py, test_atlas_campaign_push_router.py).
"""
import time
from datetime import date

from fastapi.testclient import TestClient

import app.routers.zoom_call_sync as router_mod
from app.main import app


class _FakeDB:
    """sync_zoom_calls itself is mocked in these tests -- _run_sync just
    needs a db-shaped object that survives get_session_factory()() and
    .close(), not a real session/connection."""

    def close(self):
        pass


def _stub_session_factory(monkeypatch):
    monkeypatch.setattr(router_mod, "get_session_factory", lambda: (lambda: _FakeDB()))


def _wait_for_job(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    status_response = client.get(f"/tasks/zoom-call-sync/run/{job_id}").json()
    while status_response["job_status"] == "running":
        if time.time() > deadline:
            raise TimeoutError(f"job {job_id} still running after {timeout}s")
        time.sleep(0.01)
        status_response = client.get(f"/tasks/zoom-call-sync/run/{job_id}").json()
    return status_response


def test_trigger_defaults_target_date_to_none_and_returns_job_results(monkeypatch):
    captured = {}

    def _fake(db, target_date=None):
        captured["target_date"] = target_date
        return {"target_date": "2026-09-16", "new_records": 3, "matched": 2, "skipped_no_transcript": 1, "user_errors": []}

    monkeypatch.setattr(router_mod, "sync_zoom_calls", _fake)
    _stub_session_factory(monkeypatch)
    client = TestClient(app)

    trigger_response = client.post("/tasks/zoom-call-sync/run").json()
    assert trigger_response["job_status"] == "running"

    body = _wait_for_job(client, trigger_response["job_id"])

    assert captured["target_date"] is None
    assert body["job_status"] == "done"
    assert body["new_records"] == 3
    assert body["matched"] == 2


def test_trigger_passes_through_an_explicit_target_date(monkeypatch):
    captured = {}

    def _fake(db, target_date=None):
        captured["target_date"] = target_date
        return {"target_date": str(target_date), "new_records": 0, "matched": 0, "skipped_no_transcript": 0, "user_errors": []}

    monkeypatch.setattr(router_mod, "sync_zoom_calls", _fake)
    _stub_session_factory(monkeypatch)
    client = TestClient(app)

    trigger_response = client.post("/tasks/zoom-call-sync/run", params={"target_date": "2026-09-10"}).json()
    _wait_for_job(client, trigger_response["job_id"])

    assert captured["target_date"] == date(2026, 9, 10)


def test_unknown_job_id_returns_404():
    client = TestClient(app)
    resp = client.get("/tasks/zoom-call-sync/run/does-not-exist")
    assert resp.status_code == 404
