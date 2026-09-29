"""
Tests POST /admin-tasks/create -- the admin-password gate (same convention as
atlas_report.py's override endpoints, see test_atlas_report_router.py) and
that a ValueError from create_admin_task (unknown account, no folder, no
Delivery list) surfaces as a 400 with the real message, not a 500.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.admin_task_create as router_mod

_ADMIN_HEADERS = {"X-Admin-Password": router_mod._ADMIN_PASSWORD}


def _client():
    app = FastAPI()
    app.include_router(router_mod.router)
    return TestClient(app)


def test_missing_admin_password_is_rejected(monkeypatch):
    client = _client()
    resp = client.post("/admin-tasks/create", json={"atlas_id": "a", "title": "t"})
    assert resp.status_code == 401


def test_wrong_admin_password_is_rejected():
    client = _client()
    resp = client.post("/admin-tasks/create", json={"atlas_id": "a", "title": "t"}, headers={"X-Admin-Password": "nope"})
    assert resp.status_code == 401


def test_successful_creation_returns_the_task(monkeypatch):
    monkeypatch.setattr(router_mod, "create_admin_task", lambda atlas_id, title, **kw: {"clickupTaskId": "t-1", "title": title})
    client = _client()

    resp = client.post("/admin-tasks/create", json={"atlas_id": "atlas-1", "title": "Confirm domain access"}, headers=_ADMIN_HEADERS)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "task": {"clickupTaskId": "t-1", "title": "Confirm domain access"}}


def test_value_error_from_create_admin_task_surfaces_as_400(monkeypatch):
    def _raise(atlas_id, title, **kw):
        raise ValueError("no ClickUp folder on file")
    monkeypatch.setattr(router_mod, "create_admin_task", _raise)
    client = _client()

    resp = client.post("/admin-tasks/create", json={"atlas_id": "atlas-1", "title": "Task"}, headers=_ADMIN_HEADERS)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "no ClickUp folder on file"
