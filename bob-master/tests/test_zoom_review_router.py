"""
Tests /admin/zoom-calls -- the manual transcript-to-client assignment UI.
Same isolated-app + real temp-file SQLite convention as
test_atlas_report_router.py (not app.main, which starts a real
BackgroundScheduler and needs a real DATABASE_URL).
"""
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routers.zoom_review as router_mod
from app.db import Base, get_db
from app.models import ZoomCallRecord


class _FakeAtlasClient:
    accounts = [
        {"id": "acme-1", "companyName": "Acme Co", "isActive": True},
        {"id": "zeta-1", "companyName": "Zeta LLC", "isActive": True},
        {"id": "old-1", "companyName": "Retired Client", "isActive": False},
    ]

    def __init__(self, *a, **kw):
        pass

    def get_all_accounts(self):
        return _FakeAtlasClient.accounts


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(router_mod, "AtlasClient", _FakeAtlasClient)
    engine = create_engine(f"sqlite:///{tmp_path / 'zoom_review.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_db] = lambda: session_factory()
    return TestClient(app), session_factory


def _seed(session_factory, **overrides):
    defaults = dict(
        meeting_uuid="uuid-1", host_email="tim@x.com", topic="AM x Acme Co",
        start_time=datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc),
        atlas_account_id=None, matched_company_name=None, match_confidence=None,
        manually_assigned=False, assigned_by=None, assigned_at=None,
        transcript_text="hello world", pulled_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    db = session_factory()
    row = ZoomCallRecord(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    db.close()
    return row.id


def test_default_filter_shows_only_unassigned_calls(tmp_path, monkeypatch):
    client, session_factory = _client(tmp_path, monkeypatch)
    _seed(session_factory, meeting_uuid="uuid-1", topic="Unmatched call")
    _seed(session_factory, meeting_uuid="uuid-2", topic="Matched call",
          atlas_account_id="acme-1", matched_company_name="Acme Co", match_confidence=0.9)

    resp = client.get("/admin/zoom-calls")

    assert resp.status_code == 200
    assert "Unmatched call" in resp.text
    assert "Matched call" not in resp.text


def test_auto_matched_filter_excludes_manually_assigned(tmp_path, monkeypatch):
    client, session_factory = _client(tmp_path, monkeypatch)
    _seed(session_factory, meeting_uuid="uuid-1", topic="Auto call",
          atlas_account_id="acme-1", matched_company_name="Acme Co", match_confidence=0.9)
    _seed(session_factory, meeting_uuid="uuid-2", topic="Manual call",
          atlas_account_id="zeta-1", matched_company_name="Zeta LLC", manually_assigned=True, assigned_by="chris")

    resp = client.get("/admin/zoom-calls", params={"filter": "auto_matched"})

    assert "Auto call" in resp.text
    assert "Manual call" not in resp.text


def test_search_filters_by_topic(tmp_path, monkeypatch):
    client, session_factory = _client(tmp_path, monkeypatch)
    _seed(session_factory, meeting_uuid="uuid-1", topic="Onboarding kickoff")
    _seed(session_factory, meeting_uuid="uuid-2", topic="Renewal chat")

    resp = client.get("/admin/zoom-calls", params={"filter": "all", "q": "kickoff"})

    assert "Onboarding kickoff" in resp.text
    assert "Renewal chat" not in resp.text


def test_assign_sets_atlas_account_and_is_flagged_manual(tmp_path, monkeypatch):
    client, session_factory = _client(tmp_path, monkeypatch)
    call_id = _seed(session_factory, meeting_uuid="uuid-1", topic="Unmatched call")

    resp = client.post(
        f"/admin/zoom-calls/{call_id}/assign",
        data={"atlas_account_id": "zeta-1", "assigned_by": "chris", "filter": "unassigned", "q": ""},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/zoom-calls?filter=unassigned"

    db = session_factory()
    row = db.get(ZoomCallRecord, call_id)
    assert row.atlas_account_id == "zeta-1"
    assert row.matched_company_name == "Zeta LLC"
    assert row.manually_assigned is True
    assert row.assigned_by == "chris"
    assert row.assigned_at is not None


def test_assign_then_future_context_query_picks_it_up(tmp_path, monkeypatch):
    """Proves the assignment lands in the same column every existing
    consumer (_add_zoom_context, daily_go_live_audit.py) already filters
    on -- no separate read path needed for future blends to see it."""
    client, session_factory = _client(tmp_path, monkeypatch)
    call_id = _seed(session_factory, meeting_uuid="uuid-1", topic="Unmatched call")

    client.post(
        f"/admin/zoom-calls/{call_id}/assign",
        data={"atlas_account_id": "zeta-1", "assigned_by": "chris", "filter": "unassigned", "q": ""},
    )

    db = session_factory()
    matches = db.query(ZoomCallRecord).filter(ZoomCallRecord.atlas_account_id == "zeta-1").all()
    assert len(matches) == 1
    assert matches[0].id == call_id


def test_clear_resets_assignment_back_to_unassigned(tmp_path, monkeypatch):
    client, session_factory = _client(tmp_path, monkeypatch)
    call_id = _seed(session_factory, meeting_uuid="uuid-1", topic="Manual call",
                     atlas_account_id="zeta-1", matched_company_name="Zeta LLC",
                     manually_assigned=True, assigned_by="chris", assigned_at=datetime.now(timezone.utc))

    resp = client.post(
        f"/admin/zoom-calls/{call_id}/clear",
        data={"filter": "all", "q": ""},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    db = session_factory()
    row = db.get(ZoomCallRecord, call_id)
    assert row.atlas_account_id is None
    assert row.manually_assigned is False
    assert row.assigned_by is None


def test_assign_unknown_call_id_redirects_without_error(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    resp = client.post(
        "/admin/zoom-calls/999/assign",
        data={"atlas_account_id": "zeta-1", "assigned_by": "chris", "filter": "unassigned", "q": ""},
        follow_redirects=False,
    )

    assert resp.status_code == 303


def test_atlas_fetch_failure_shows_banner_but_still_lists_calls(tmp_path, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("atlas is down")

    monkeypatch.setattr(router_mod, "AtlasClient", _boom)
    engine = create_engine(f"sqlite:///{tmp_path / 'zoom_review_err.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_db] = lambda: session_factory()
    client = TestClient(app)
    _seed(session_factory, meeting_uuid="uuid-1", topic="Unmatched call")

    resp = client.get("/admin/zoom-calls")

    assert resp.status_code == 200
    assert "atlas is down" in resp.text
    assert "Unmatched call" in resp.text
