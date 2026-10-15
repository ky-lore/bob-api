"""SalesHostMiddleware: the sales hostname is a narrow view (root = dashboard, nothing else), other hosts untouched."""
import json
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routers.sales_audit as router_mod
from app.config import get_settings
from app.db import Base, get_db
from app.models import SalesAuditRun
from app.sales_host import SalesHostMiddleware

SALES = "sales.advancedmarketers.co"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SALES_DASHBOARD_HOST", SALES)
    get_settings.cache_clear()
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(bind=engine)
    sf = sessionmaker(bind=engine)
    with sf() as db:
        db.add(SalesAuditRun(run_at=datetime(2026, 10, 15, 12), report_json=json.dumps(
            {"generated_at": "2026-10-15T12:00:00+00:00", "count": 0, "rows": [], "summary": {}, "source_errors": {}, "rules": {}})))
        db.commit()
    app = FastAPI()
    app.add_middleware(SalesHostMiddleware)
    app.include_router(router_mod.router)
    app.dependency_overrides[get_db] = lambda: sf()

    @app.get("/")
    def other_root():
        return {"app": "bob-root"}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/reports/atlas-account-status/pulse")
    def pulse():
        return {"secret": "pulse"}

    @app.post("/tasks/daily-go-live-audit/run")
    def trigger():
        return {"triggered": True}

    yield TestClient(app)
    get_settings.cache_clear()


def _get(c, path, host, **kw):
    return c.get(path, headers={"host": host}, **kw)


def test_root_on_the_sales_host_is_the_dashboard(client):
    r = _get(client, "/", SALES)
    assert r.status_code == 200 and "Sales Audit" in r.text and "bob-root" not in r.text


def test_dashboard_subroutes_and_health_work_on_the_sales_host(client):
    assert _get(client, "/reports/sales-audit", SALES).status_code == 200
    assert _get(client, "/reports/sales-audit/latest", SALES).status_code == 200
    assert _get(client, "/reports/sales-audit/view/1", SALES).status_code == 200
    assert _get(client, "/health", SALES).json() == {"status": "ok"}


def test_everything_else_is_404_on_the_sales_host_including_triggers(client):
    assert _get(client, "/reports/atlas-account-status/pulse", SALES).status_code == 404
    assert client.post("/tasks/daily-go-live-audit/run", headers={"host": SALES}).status_code == 404
    assert _get(client, "/docs", SALES).status_code == 404
    assert _get(client, "/reports/sales-auditXYZ", SALES).status_code == 404            # prefix must be a real path boundary


def test_other_hosts_are_completely_untouched(client):
    other = "bob-api-production-9ba0.up.railway.app"
    assert _get(client, "/", other).json() == {"app": "bob-root"}                        # root NOT rewritten
    assert _get(client, "/reports/atlas-account-status/pulse", other).json() == {"secret": "pulse"}
    assert client.post("/tasks/daily-go-live-audit/run", headers={"host": other}).json() == {"triggered": True}


def test_host_match_is_case_and_port_insensitive_and_honours_forwarded_host(client):
    assert _get(client, "/", "SALES.AdvancedMarketers.co:443").status_code == 200
    r = client.get("/", headers={"host": "internal.railway", "x-forwarded-host": SALES})
    assert r.status_code == 200 and "Sales Audit" in r.text


def test_empty_setting_turns_the_feature_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SALES_DASHBOARD_HOST", "")
    get_settings.cache_clear()
    app = FastAPI()
    app.add_middleware(SalesHostMiddleware)

    @app.get("/")
    def root():
        return {"app": "bob-root"}

    assert TestClient(app).get("/", headers={"host": SALES}).json() == {"app": "bob-root"}
    get_settings.cache_clear()
