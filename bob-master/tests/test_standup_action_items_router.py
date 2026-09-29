"""
Tests the /tasks/standup-action-items/run routes -- proves they're mounted on
app.main and kick sync_standup_action_items off on job_tracker's background
thread, same job_id/poll shape as every other background task in this app
(see test_zoom_call_sync_router.py, test_trigger_endpoint.py). No DB session
involved here (unlike zoom_call_sync's router) since this task never
persists anything in Bob's own Postgres -- it only relays ClickUp -> Atlas.
"""
import time

from fastapi.testclient import TestClient

import app.routers.standup_action_items as router_mod
from app.main import app


def _wait_for_job(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    status_response = client.get(f"/tasks/standup-action-items/run/{job_id}").json()
    while status_response["job_status"] == "running":
        if time.time() > deadline:
            raise TimeoutError(f"job {job_id} still running after {timeout}s")
        time.sleep(0.01)
        status_response = client.get(f"/tasks/standup-action-items/run/{job_id}").json()
    return status_response


def test_trigger_defaults_name_filter_to_admin_and_returns_job_results(monkeypatch):
    captured = {}

    def _fake(name_filter="admin"):
        captured["name_filter"] = name_filter
        return {"tasks_found": 3, "accounts_matched": 2, "accounts_unmatched": 0, "pushed": 3, "user_errors": []}

    monkeypatch.setattr(router_mod, "sync_standup_action_items", _fake)
    client = TestClient(app)

    trigger_response = client.post("/tasks/standup-action-items/run").json()
    assert trigger_response["job_status"] == "running"

    body = _wait_for_job(client, trigger_response["job_id"])

    assert captured["name_filter"] == "admin"
    assert body["job_status"] == "done"
    assert body["pushed"] == 3


def test_trigger_passes_through_a_custom_name_filter(monkeypatch):
    captured = {}

    def _fake(name_filter="admin"):
        captured["name_filter"] = name_filter
        return {"tasks_found": 0, "accounts_matched": 0, "accounts_unmatched": 0, "pushed": 0, "user_errors": []}

    monkeypatch.setattr(router_mod, "sync_standup_action_items", _fake)
    client = TestClient(app)

    trigger_response = client.post("/tasks/standup-action-items/run", params={"name_filter": "custom-tag"}).json()
    _wait_for_job(client, trigger_response["job_id"])

    assert captured["name_filter"] == "custom-tag"


def test_unknown_job_id_returns_404():
    client = TestClient(app)
    resp = client.get("/tasks/standup-action-items/run/does-not-exist")
    assert resp.status_code == 404
