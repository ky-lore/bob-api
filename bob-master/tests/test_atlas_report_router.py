"""
Tests the GET /reports/atlas-account-status route itself -- proves it's
actually mounted on app.main and returns build_atlas_report's shape, not
just that build_atlas_report works in isolation (see test_atlas_report.py).
"""
from fastapi.testclient import TestClient

import app.routers.atlas_report as router_mod
from app.main import app


def test_endpoint_is_mounted_and_returns_the_report_shape(monkeypatch):
    monkeypatch.setattr(
        router_mod,
        "build_atlas_report",
        lambda limit=None: (
            [{"atlas_id": "acme-123", "company_name": "Acme Co", "status": "ok", "recent_work": "did stuff"}],
            [{"batch_index": 0, "ok": True}],
        ),
    )
    client = TestClient(app)

    resp = client.get("/reports/atlas-account-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["accounts"][0]["atlas_id"] == "acme-123"
    assert body["narrative_batches"][0]["ok"] is True


def test_limit_query_param_is_passed_through(monkeypatch):
    captured = {}

    def _fake(limit=None):
        captured["limit"] = limit
        return [], []

    monkeypatch.setattr(router_mod, "build_atlas_report", _fake)
    client = TestClient(app)

    client.get("/reports/atlas-account-status", params={"limit": 5})

    assert captured["limit"] == 5
