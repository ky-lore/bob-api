"""
Tests the GET /reports/atlas-account-status route itself -- proves it's
actually mounted, wires the db session through to build_atlas_report (needed
now for the Zoom-context lookup, 2026-09-18), and returns build_atlas_report's
shape. Uses an isolated FastAPI app (just this router) with get_db overridden
to a real temp-file SQLite session, same convention as test_dashboard_route.py
-- not app.main, which starts a real BackgroundScheduler singleton unsafe to
start twice per process (see test_trigger_endpoint.py), and whose real
Depends(get_db) would otherwise reach for a real DATABASE_URL.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routers.atlas_report as router_mod
from app.db import Base, get_db


def _client(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_db] = lambda: session_factory()
    return TestClient(app)


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
