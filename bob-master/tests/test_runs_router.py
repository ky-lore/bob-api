"""
Tests /admin/runs -- the manual Pulse/CMDCTR trigger page. No backend
dependencies of its own (renders a static shell; all actual work happens
client-side against the existing job_id/poll endpoints), so this just
proves the route is mounted and the page references the real endpoints
it's meant to drive.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.runs as router_mod


def _client():
    app = FastAPI()
    app.include_router(router_mod.router)
    return TestClient(app)


def test_runs_page_is_mounted_and_renders():
    resp = _client().get("/admin/runs")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_runs_page_references_the_real_trigger_and_poll_endpoints():
    body = _client().get("/admin/runs").text

    assert "/reports/atlas-account-status/run" in body
    assert "/reports/atlas-account-status/latest" in body
    assert "/reports/cmdctr/run" in body
    assert "/reports/cmdctr/latest" in body


def test_runs_page_has_a_button_for_each_run_kind():
    body = _client().get("/admin/runs").text

    assert 'onclick="runJob(\'pulse\')"' in body
    assert 'onclick="runJob(\'cmdctr\')"' in body
