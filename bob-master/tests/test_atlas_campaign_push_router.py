"""
Tests the /tasks/atlas-campaign-push/run routes themselves -- proves they're
mounted on app.main, kick push_campaign_spend off on job_tracker's background
thread (same job_id/poll shape as the daily-go-live-audit trigger, see
test_trigger_endpoint.py), and that dry_run defaults to True so a bare POST
never touches real Atlas.
"""
import time

from fastapi.testclient import TestClient

import app.routers.atlas_campaign_push as router_mod
from app.main import app


def _wait_for_job(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    status_response = client.get(f"/tasks/atlas-campaign-push/run/{job_id}").json()
    while status_response["job_status"] == "running":
        if time.time() > deadline:
            raise TimeoutError(f"job {job_id} still running after {timeout}s")
        time.sleep(0.01)
        status_response = client.get(f"/tasks/atlas-campaign-push/run/{job_id}").json()
    return status_response


def test_trigger_defaults_to_dry_run_and_passes_through_to_push_campaign_spend(monkeypatch):
    captured = {}

    def _fake(limit=None, dry_run=True):
        captured["limit"] = limit
        captured["dry_run"] = dry_run
        return [{"atlas_id": "acme-1", "platform": "google", "ok": True, "error": None, "payload": {}}]

    monkeypatch.setattr(router_mod, "push_campaign_spend", _fake)
    client = TestClient(app)

    trigger_response = client.post("/tasks/atlas-campaign-push/run").json()
    assert trigger_response["job_status"] == "running"
    assert trigger_response["dry_run"] is True

    body = _wait_for_job(client, trigger_response["job_id"])

    assert captured["dry_run"] is True  # never opted into a real push by default
    assert captured["limit"] is None
    assert body["job_status"] == "done"
    assert body["dry_run"] is True
    assert body["results"][0]["atlas_id"] == "acme-1"


def test_trigger_passes_through_dry_run_false_and_limit(monkeypatch):
    captured = {}

    def _fake(limit=None, dry_run=True):
        captured["limit"] = limit
        captured["dry_run"] = dry_run
        return []

    monkeypatch.setattr(router_mod, "push_campaign_spend", _fake)
    client = TestClient(app)

    trigger_response = client.post("/tasks/atlas-campaign-push/run", params={"dry_run": "false", "limit": 3}).json()
    assert trigger_response["dry_run"] is False

    _wait_for_job(client, trigger_response["job_id"])

    assert captured["dry_run"] is False
    assert captured["limit"] == 3


def test_unknown_job_id_returns_404():
    client = TestClient(app)
    resp = client.get("/tasks/atlas-campaign-push/run/does-not-exist")
    assert resp.status_code == 404
