"""
Renders dashboard.html for real over HTTP (TestClient against a fresh
FastAPI app with just the dashboard router mounted -- not app.main, which
starts a real BackgroundScheduler singleton unsafe to start twice per
process, see test_trigger_endpoint.py) with data exercising every branch
touched by the 2026-08-11 visual refresh (collapsible <details> sections,
colored day counts, the day==0 "signed today" badge, all four ads_off
buckets, the accounts chart) plus the recommended-action checkoff feature
added the same day -- template-only changes have no other test coverage,
and a bad Jinja expression only surfaces at render time.
"""
import json
from datetime import date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.models import ActionItemCheckoff, AuditRun, Flag, FlagCategory, RunStatus
from app.routers import dashboard


def _dashboard_json() -> str:
    return json.dumps({
        "stat_tiles": {
            "accounts_tracked": 3, "live": 1, "not_live": 2, "behind": 1,
            "should_be_on_but_dark": 1, "campaigns_on_zero_spend": 1, "verified_off": 1,
        },
        "accounts_overview": [
            {
                "account": "Amaral's Construction", "day": 40, "is_live": False, "target_status": "behind",
                "ad_spend": {"Meta": {"spend": 0.0, "enabled_campaigns": 1, "campaigns": []}},
                "ad_spend_errors": {}, "status": "Client ghosting, escalation overdue.",
                "recommended_action": "Escalate: call the client today, ghosting for 2 weeks.",
            },
            {
                "account": "Forbes Landscape", "day": 24, "is_live": False, "target_status": "approaching",
                "ad_spend": {}, "ad_spend_errors": {"Google Ads": "403 forbidden"},
                "status": "Meta POC overdue per card.",
                "recommended_action": "No action needed",
            },
            {
                "account": "Inland Renovation", "day": 0, "is_live": True, "target_status": "live",
                "ad_spend": {}, "ad_spend_errors": {}, "status": "Signed today.",
                "recommended_action": "No action needed",
            },
        ],
        "accounts_chart": [
            {"account": "Amaral's Construction", "day": 40, "target_status": "behind"},
            {"account": "Forbes Landscape", "day": 24, "target_status": "approaching"},
            {"account": "Inland Renovation", "day": 0, "target_status": "live"},
        ],
        "ads_off": {
            "should_be_on_but_dark": [{"account": "Roof City", "detail": "0 campaigns both platforms."}],
            "campaigns_on_zero_spend": [{"account": "5blox", "platform": "Meta", "note": "83 campaigns, $0."}],
            "unsettled": [{"account": "Sierra Trimlight", "note": "Card declining."}],
            "verified_off": [{"account": "Ram Dumpster", "note": "Cancelled, confirmed off."}],
        },
        "new_deals": [{"account": "M.L. Berni Co.", "note": "Signed today."}],
        "went_live": [{"account": "Lincoln Plumbing"}],
        "web_builds": [{"name": "ColdRiite build", "day": 374, "status": "In progress"}],
        "narrative_error": None,
    })


def _build_app(tmp_path, db_name="dashboard_route.db"):
    """Seeds one AuditRun (+ a matching Flag) into a fresh temp-file sqlite
    DB and returns (app, session_factory, run_id) -- shared setup for every
    test in this file."""
    db_path = tmp_path / db_name
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    db = session_factory()
    run = AuditRun(
        run_date=date(2026, 8, 11),
        started_at=datetime(2026, 8, 11, 7, 0),
        finished_at=datetime(2026, 8, 11, 7, 20),
        status=RunStatus.partial,
        notes="DEBUG: full stress test run",
        dashboard_json=_dashboard_json(),
    )
    db.add(run)
    db.flush()
    db.add(Flag(
        run_id=run.id, category=FlagCategory.new_deal, client_name="M.L. Berni Co.",
        message="Signed today.", created_at=datetime(2026, 8, 11, 7, 0),
    ))
    db.commit()
    run_id = run.id
    db.close()

    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[get_db] = lambda: session_factory()
    return app, session_factory, run_id


def test_dashboard_renders_every_section_without_a_template_error(tmp_path):
    app, _session_factory, _run_id = _build_app(tmp_path)

    response = TestClient(app).get("/dashboard")

    assert response.status_code == 200
    body = response.text
    assert "Go-Live Pipeline Dashboard" in body
    assert "theme-toggle" in body
    assert 'class="chevron"' in body
    # day==0 account gets the "signed today" badge
    assert "Inland Renovation" in body and "🆕" in body
    # behind/approaching day counts get their color class
    assert 'class="day day--behind"' in body
    assert 'class="day day--approaching"' in body
    assert 'class="day day--live"' in body
    # all four ads_off buckets rendered
    for account in ("Roof City", "5blox", "Sierra Trimlight", "Ram Dumpster"):
        assert account in body


def test_recommended_action_renders_checkbox_only_when_there_is_a_real_action(tmp_path):
    app, _session_factory, _run_id = _build_app(tmp_path)

    body = TestClient(app).get("/dashboard").text

    # Exactly one real action across the three accounts -> exactly one checkbox.
    # "action-item-checkbox" alone isn't a safe marker -- it also appears once
    # in the page's own <style> rule and once in the <script> selector,
    # regardless of how many real actions exist. data-run-id is only ever
    # emitted on an actual rendered <input>, one per real action item.
    assert body.count('data-run-id="') == 1
    assert "Escalate: call the client today" in body
    assert body.count("No action needed") == 2


def test_toggle_action_item_checks_then_unchecks(tmp_path):
    app, session_factory, run_id = _build_app(tmp_path)
    client = TestClient(app)
    payload = {"run_id": run_id, "account_name": "Amaral's Construction"}

    check_response = client.post("/dashboard/action-items/toggle", json=payload)
    assert check_response.status_code == 200
    assert check_response.json() == {"checked": True}

    db = session_factory()
    try:
        row = db.query(ActionItemCheckoff).filter_by(run_id=run_id, account_name="Amaral's Construction").first()
        assert row is not None
    finally:
        db.close()

    # Checked state reflects on the next render -- struck-through label class applied
    body_after_check = client.get("/dashboard").text
    assert "action-item-label--checked" in body_after_check

    uncheck_response = client.post("/dashboard/action-items/toggle", json=payload)
    assert uncheck_response.status_code == 200
    assert uncheck_response.json() == {"checked": False}

    db = session_factory()
    try:
        row = db.query(ActionItemCheckoff).filter_by(run_id=run_id, account_name="Amaral's Construction").first()
        assert row is None
    finally:
        db.close()


def test_toggle_action_item_is_scoped_per_run_not_shared_across_runs(tmp_path):
    app, session_factory, run_id = _build_app(tmp_path)
    client = TestClient(app)

    client.post("/dashboard/action-items/toggle", json={"run_id": run_id, "account_name": "Amaral's Construction"})

    db = session_factory()
    try:
        other_run_id = run_id + 999  # a different run's checkoff must not exist
        assert db.query(ActionItemCheckoff).filter_by(run_id=other_run_id).first() is None
        assert db.query(ActionItemCheckoff).filter_by(run_id=run_id).count() == 1
    finally:
        db.close()
