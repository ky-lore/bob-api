"""
Tests the /reports/atlas-account-status routes -- the original synchronous
GET, plus the async trigger/poll/latest trio added 2026-09-18 for the full,
unbounded account universe (a real unlimited pull 502's past Railway's
~300s gateway timeout -- see the router's module docstring). Uses an
isolated FastAPI app (just this router) with get_db overridden to a real
temp-file SQLite session, same convention as test_dashboard_route.py --
not app.main, which starts a real BackgroundScheduler singleton unsafe to
start twice per process (see test_trigger_endpoint.py), and whose real
Depends(get_db) would otherwise reach for a real DATABASE_URL.
"""
import json
import re
import threading
import time
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routers.atlas_report as router_mod
from app.db import Base, get_db
from app.models import AccountHealthOverride, AtlasReportRun


class _FakeDB:
    """run_and_store_atlas_report itself is mocked in the trigger/poll tests
    below -- the background thread just needs a db-shaped object that
    survives get_session_factory()() and .close(), same convention as
    test_zoom_call_sync_router.py's _FakeDB."""

    def close(self):
        pass


def _client_and_session_factory(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_db] = lambda: session_factory()
    return TestClient(app), session_factory


def _client(tmp_path):
    client, _ = _client_and_session_factory(tmp_path)
    return client


def _wait_for_job(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    body = client.get(f"/reports/atlas-account-status/run/{job_id}").json()
    while body["job_status"] == "running":
        if time.time() > deadline:
            raise TimeoutError(f"job {job_id} still running after {timeout}s")
        time.sleep(0.01)
        body = client.get(f"/reports/atlas-account-status/run/{job_id}").json()
    return body


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


class _FakeRun:
    def __init__(self, run_id, report_json):
        self.id = run_id
        self.run_at = datetime(2026, 9, 18, 12, 0, 0)
        self.report_json = report_json


def test_trigger_starts_a_background_job_and_returns_its_result_on_poll(monkeypatch, tmp_path):
    captured = {}

    def _fake_run_and_store(db, limit=None, on_progress=None):
        captured["limit"] = limit
        return _FakeRun(7, json.dumps({"count": 1, "accounts": [{"company_name": "Acme Co"}], "narrative_batches": []}))

    monkeypatch.setattr(router_mod, "run_and_store_atlas_report", _fake_run_and_store)
    monkeypatch.setattr(router_mod, "get_session_factory", lambda: (lambda: _FakeDB()))
    client = _client(tmp_path)

    trigger_response = client.post("/reports/atlas-account-status/run", params={"limit": 20}).json()
    assert trigger_response["job_status"] == "running"

    body = _wait_for_job(client, trigger_response["job_id"])

    assert captured["limit"] == 20
    assert body["job_status"] == "done"
    assert body["run_id"] == 7
    assert body["count"] == 1
    assert body["accounts"][0]["company_name"] == "Acme Co"


def test_trigger_job_error_surfaces_on_poll(monkeypatch, tmp_path):
    def _always_fail(db, limit=None, on_progress=None):
        raise RuntimeError("Atlas pull failed")

    monkeypatch.setattr(router_mod, "run_and_store_atlas_report", _always_fail)
    monkeypatch.setattr(router_mod, "get_session_factory", lambda: (lambda: _FakeDB()))
    client = _client(tmp_path)

    trigger_response = client.post("/reports/atlas-account-status/run").json()
    body = _wait_for_job(client, trigger_response["job_id"])

    assert body["job_status"] == "error"
    assert "Atlas pull failed" in body["error"]


def test_progress_is_visible_on_poll_while_the_job_is_still_running(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()

    def _slow_run_and_store(db, limit=None, on_progress=None):
        on_progress({"phase": "gathering", "completed": 12, "total": 148, "account": "Acme Co"})
        started.set()
        release.wait(timeout=5.0)
        return _FakeRun(9, json.dumps({"count": 0, "accounts": [], "narrative_batches": []}))

    monkeypatch.setattr(router_mod, "run_and_store_atlas_report", _slow_run_and_store)
    monkeypatch.setattr(router_mod, "get_session_factory", lambda: (lambda: _FakeDB()))
    client = _client(tmp_path)

    trigger_response = client.post("/reports/atlas-account-status/run").json()
    started.wait(timeout=5.0)

    mid_run = client.get(f"/reports/atlas-account-status/run/{trigger_response['job_id']}").json()
    assert mid_run["job_status"] == "running"
    assert mid_run["progress"] == {"phase": "gathering", "completed": 12, "total": 148, "account": "Acme Co"}

    release.set()
    _wait_for_job(client, trigger_response["job_id"])


def test_unknown_job_id_returns_404(tmp_path):
    resp = _client(tmp_path).get("/reports/atlas-account-status/run/nonexistent")
    assert resp.status_code == 404


def test_latest_reads_the_most_recently_persisted_run_directly_from_the_db(tmp_path):
    client, session_factory = _client_and_session_factory(tmp_path)
    db = session_factory()
    db.add(AtlasReportRun(
        run_at=datetime(2026, 9, 17, 9, 0, 0),
        limit_used=None,
        report_json=json.dumps({"count": 1, "accounts": [{"company_name": "Older Run Co"}], "narrative_batches": []}),
    ))
    db.add(AtlasReportRun(
        run_at=datetime(2026, 9, 18, 9, 0, 0),
        limit_used=None,
        report_json=json.dumps({"count": 1, "accounts": [{"company_name": "Latest Run Co"}], "narrative_batches": []}),
    ))
    db.commit()
    db.close()

    resp = client.get("/reports/atlas-account-status/latest")

    assert resp.status_code == 200
    body = resp.json()
    assert body["accounts"][0]["company_name"] == "Latest Run Co"


def test_latest_with_no_runs_yet_returns_an_empty_shape_not_an_error(tmp_path):
    resp = _client(tmp_path).get("/reports/atlas-account-status/latest")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"run_id": None, "run_at": None, "count": 0, "accounts": [], "narrative_batches": []}


def _sample_account(name, health, **overrides):
    base = {
        "atlas_id": name.lower().replace(" ", "-"),
        "company_name": name,
        "stage": "live",
        "day": 90,
        "is_live": True,
        "health": health,
        "status": f"{name} status sentence.",
        "recent_work": f"{name} recent work sentence.",
        "google_ads": {"total_cost": 123.0},
        "google_ads_error": None,
        "meta_ads": None,
        "meta_ads_error": None,
        "ad_spend": {"total_spend": 123.0, "total_conversions": 2.0, "cost_per_conversion": 61.5},
        "zoom_call_count": 0,
    }
    base.update(overrides)
    return base


def test_pulse_with_no_run_yet_shows_the_empty_state(tmp_path):
    resp = _client(tmp_path).get("/reports/atlas-account-status/pulse")

    assert resp.status_code == 200
    assert "No report run yet" in resp.text


def test_pulse_renders_accounts_sorted_by_severity_with_health_counts(tmp_path):
    client, session_factory = _client_and_session_factory(tmp_path)
    db = session_factory()
    accounts = [
        _sample_account("On Track Co", "on_track"),
        _sample_account("At Risk Co", "at_risk"),
        _sample_account("Needs Attention Co", "needs_attention"),
    ]
    db.add(AtlasReportRun(
        run_at=datetime(2026, 9, 18, 12, 0, 0),
        limit_used=None,
        report_json=json.dumps({"count": 3, "accounts": accounts, "narrative_batches": []}),
    ))
    db.commit()
    db.close()

    resp = client.get("/reports/atlas-account-status/pulse")

    assert resp.status_code == 200
    body = resp.text
    # At-risk sorts first, on-track last -- urgency order, not input order.
    assert body.index("At Risk Co") < body.index("Needs Attention Co") < body.index("On Track Co")
    assert "At Risk Co status sentence." in body
    assert "At Risk Co recent work sentence." in body
    # No leaked template artifacts.
    assert "None" not in body
    assert "{{" not in body and "{%" not in body


def test_pulse_shows_no_spend_data_and_no_ad_platform_gracefully(tmp_path):
    client, session_factory = _client_and_session_factory(tmp_path)
    db = session_factory()
    account = _sample_account(
        "No Spend Co", "on_track",
        google_ads=None, google_ads_error=None, meta_ads=None, meta_ads_error=None, ad_spend=None,
    )
    db.add(AtlasReportRun(
        run_at=datetime(2026, 9, 18, 12, 0, 0),
        limit_used=None,
        report_json=json.dumps({"count": 1, "accounts": [account], "narrative_batches": []}),
    ))
    db.commit()
    db.close()

    resp = client.get("/reports/atlas-account-status/pulse")

    assert resp.status_code == 200
    assert "no spend data" in resp.text
    assert "No ad platform on file" in resp.text


def test_pulse_for_a_specific_run_id_renders_that_historical_run(tmp_path):
    client, session_factory = _client_and_session_factory(tmp_path)
    db = session_factory()
    older = AtlasReportRun(
        run_at=datetime(2026, 9, 17, 9, 0, 0),
        limit_used=None,
        report_json=json.dumps({"count": 1, "accounts": [_sample_account("Older Run Co", "on_track")], "narrative_batches": []}),
    )
    newer = AtlasReportRun(
        run_at=datetime(2026, 9, 18, 9, 0, 0),
        limit_used=None,
        report_json=json.dumps({"count": 1, "accounts": [_sample_account("Newer Run Co", "on_track")], "narrative_batches": []}),
    )
    db.add(older)
    db.add(newer)
    db.commit()
    db.refresh(older)
    older_id = older.id
    db.close()

    resp = client.get(f"/reports/atlas-account-status/pulse/{older_id}")

    assert resp.status_code == 200
    assert "Older Run Co" in resp.text
    assert "Newer Run Co" not in resp.text


_ADMIN_HEADERS = {"X-Admin-Password": router_mod._ADMIN_OVERRIDE_PASSWORD}


def _sample_run_accounts():
    return [{
        "atlas_id": "acme-1", "company_name": "Acme Co", "stage": "live", "day": 90, "is_live": True,
        "health": "on_track", "status": "fine", "recent_work": "nothing", "google_ads": None,
        "google_ads_error": None, "meta_ads": None, "meta_ads_error": None, "ad_spend": None,
        "zoom_call_count": 0, "health_overridden": False, "health_override_reason": None,
    }]


def test_set_override_requires_correct_password(tmp_path):
    client = _client(tmp_path)
    body = {"atlas_id": "acme-1", "company_name": "Acme Co", "health": "at_risk"}

    no_header = client.post("/reports/atlas-account-status/overrides", json=body)
    assert no_header.status_code == 401

    wrong_password = client.post(
        "/reports/atlas-account-status/overrides", json=body, headers={"X-Admin-Password": "nope"}
    )
    assert wrong_password.status_code == 401

    correct = client.post("/reports/atlas-account-status/overrides", json=body, headers=_ADMIN_HEADERS)
    assert correct.status_code == 200


def test_set_override_rejects_an_invalid_health_value(tmp_path):
    client = _client(tmp_path)

    resp = client.post(
        "/reports/atlas-account-status/overrides",
        json={"atlas_id": "acme-1", "company_name": "Acme Co", "health": "somewhat_bad"},
        headers=_ADMIN_HEADERS,
    )

    assert resp.status_code == 422


def test_set_override_upserts_rather_than_duplicating(tmp_path):
    client, session_factory = _client_and_session_factory(tmp_path)
    headers = _ADMIN_HEADERS

    client.post(
        "/reports/atlas-account-status/overrides",
        json={"atlas_id": "acme-1", "company_name": "Acme Co", "health": "at_risk", "reason": "first"},
        headers=headers,
    )
    client.post(
        "/reports/atlas-account-status/overrides",
        json={"atlas_id": "acme-1", "company_name": "Acme Co", "health": "needs_attention", "reason": "second"},
        headers=headers,
    )

    db = session_factory()
    rows = db.query(AccountHealthOverride).filter_by(atlas_id="acme-1").all()
    db.close()
    assert len(rows) == 1
    assert rows[0].health == "needs_attention"
    assert rows[0].reason == "second"


def test_override_immediately_shows_on_pulse_without_a_new_run(tmp_path):
    client, session_factory = _client_and_session_factory(tmp_path)
    db = session_factory()
    db.add(AtlasReportRun(
        run_at=datetime(2026, 9, 21, 9, 0, 0), limit_used=None,
        report_json=json.dumps({"count": 1, "accounts": _sample_run_accounts(), "narrative_batches": []}),
    ))
    db.commit()
    db.close()

    before = client.get("/reports/atlas-account-status/pulse")
    assert "On track" in before.text
    assert "manually set" not in before.text

    client.post(
        "/reports/atlas-account-status/overrides",
        json={"atlas_id": "acme-1", "company_name": "Acme Co", "health": "at_risk", "reason": "Known churn risk"},
        headers=_ADMIN_HEADERS,
    )

    after = client.get("/reports/atlas-account-status/pulse")
    assert "At risk" in after.text
    assert "manually set" in after.text
    assert 'value="Known churn risk"' in after.text  # reason pre-filled into the override form's input


def test_clear_override_reverts_pulse_to_the_llm_health(tmp_path):
    client, session_factory = _client_and_session_factory(tmp_path)
    db = session_factory()
    db.add(AtlasReportRun(
        run_at=datetime(2026, 9, 21, 9, 0, 0), limit_used=None,
        report_json=json.dumps({"count": 1, "accounts": _sample_run_accounts(), "narrative_batches": []}),
    ))
    db.add(AccountHealthOverride(
        atlas_id="acme-1", company_name="Acme Co", health="at_risk", reason="x", set_by=None,
        created_at=datetime(2026, 9, 21), updated_at=datetime(2026, 9, 21),
    ))
    db.commit()
    db.close()

    before = client.get("/reports/atlas-account-status/pulse")
    assert "manually set" in before.text

    clear_resp = client.delete(
        "/reports/atlas-account-status/overrides/acme-1", headers=_ADMIN_HEADERS
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["cleared"] is True

    after = client.get("/reports/atlas-account-status/pulse")
    assert "manually set" not in after.text
    assert "On track" in after.text


def test_clear_override_requires_password_too(tmp_path):
    client = _client(tmp_path)

    resp = client.delete("/reports/atlas-account-status/overrides/acme-1")

    assert resp.status_code == 401


def test_needs_extra_focus_unit():
    # Stage-based, not is_live-based (2026-09-21 fix) -- only genuine
    # pipeline stages (onboarding/development) should ever glow.
    assert router_mod._needs_extra_focus({"stage": "onboarding", "health": "at_risk"}) is True
    assert router_mod._needs_extra_focus({"stage": "Development", "health": "needs_attention"}) is True
    assert router_mod._needs_extra_focus({"stage": "live", "health": "at_risk"}) is False
    assert router_mod._needs_extra_focus({"stage": "At Risk", "health": "at_risk"}) is False
    assert router_mod._needs_extra_focus({"stage": "closed", "health": "at_risk"}) is False
    assert router_mod._needs_extra_focus({"stage": "onboarding", "health": "on_track"}) is False


def test_pipeline_and_excluded_stage_helpers_unit():
    assert router_mod._is_pipeline_stage({"stage": "Onboarding"}) is True
    assert router_mod._is_pipeline_stage({"stage": "development"}) is True
    assert router_mod._is_pipeline_stage({"stage": "live"}) is False
    assert router_mod._is_pipeline_stage({"stage": "At Risk"}) is False
    assert router_mod._is_pipeline_stage({"stage": "closed"}) is False
    assert router_mod._is_pipeline_stage({"stage": None}) is False

    assert router_mod._is_excluded_stage({"stage": "Closed"}) is True
    assert router_mod._is_excluded_stage({"stage": "closed"}) is True
    assert router_mod._is_excluded_stage({"stage": "At Risk"}) is False
    assert router_mod._is_excluded_stage({"stage": "live"}) is False


def _focus_account(name, health, is_live, day=90):
    return {
        "atlas_id": name, "company_name": name, "stage": "live" if is_live else "onboarding", "day": day,
        "is_live": is_live, "health": health, "status": f"{name} status.", "recent_work": f"{name} recent work.",
        "google_ads": None, "google_ads_error": None, "meta_ads": None, "meta_ads_error": None,
        "ad_spend": None, "zoom_call_count": 0,
    }


def test_pulse_is_split_into_a_not_live_section_and_a_live_section(tmp_path):
    """Real ask, 2026-09-21: "go-live/non-live accounts are honestly treated
    in their complete other scope... execs want to focus on those heavily."
    Pipeline (not-live) accounts get their own section, always ahead of the
    Live section, each internally sorted by severity/day -- not one merged
    list. Flagged-and-not-live cards (see _needs_extra_focus) still get the
    glow class within their section; live cards never do, regardless of
    health, since the section split itself already carries that signal."""
    client, session_factory = _client_and_session_factory(tmp_path)
    db = session_factory()
    accounts = [
        _focus_account("Live At Risk Co", "at_risk", True),
        _focus_account("Not Live At Risk Co", "at_risk", False),
        _focus_account("Live Needs Attn Co", "needs_attention", True),
        _focus_account("Not Live Needs Attn Co", "needs_attention", False),
        _focus_account("Not Live On Track Co", "on_track", False),
        _focus_account("Live On Track Co", "on_track", True),
    ]
    db.add(AtlasReportRun(
        run_at=datetime(2026, 9, 21, 9, 0, 0), limit_used=None,
        report_json=json.dumps({"count": len(accounts), "accounts": accounts, "narrative_batches": []}),
    ))
    db.commit()
    db.close()

    resp = client.get("/reports/atlas-account-status/pulse")

    assert resp.status_code == 200
    text = resp.text
    order = re.findall(r'data-company-name="([^"]+)"', text)
    # Every pipeline (not-live) account, severity/day sorted, before every
    # live account, also severity/day sorted -- not interleaved.
    assert order == [
        "Not Live At Risk Co", "Not Live Needs Attn Co", "Not Live On Track Co",
        "Live At Risk Co", "Live Needs Attn Co", "Live On Track Co",
    ]
    assert text.index("Not live yet") < text.index('data-company-name="Not Live At Risk Co"')
    assert text.index('data-company-name="Not Live At Risk Co"') < text.index(">Live<")

    # Exactly the two not-live+flagged accounts get the glow class -- not the
    # live-but-flagged ones, and not the not-live-but-on-track one. Checked
    # by looking just before each card's data-company-name attribute, where
    # the class="card health-... needs-focus" opening tag lives.
    def _card_classes(company_name):
        idx = text.index(f'data-company-name="{company_name}"')
        return text[max(0, idx - 200):idx]

    assert "needs-focus" in _card_classes("Not Live At Risk Co")
    assert "needs-focus" in _card_classes("Not Live Needs Attn Co")
    assert "needs-focus" not in _card_classes("Live At Risk Co")
    assert "needs-focus" not in _card_classes("Live Needs Attn Co")
    assert "needs-focus" not in _card_classes("Not Live On Track Co")


def _stage_account(name, stage, health="on_track", day=90):
    is_live = stage.lower() == "live"
    return {
        "atlas_id": name, "company_name": name, "stage": stage, "day": day, "is_live": is_live,
        "health": health, "status": f"{name} status.", "recent_work": f"{name} recent work.",
        "google_ads": None, "google_ads_error": None, "meta_ads": None, "meta_ads_error": None,
        "ad_spend": None, "zoom_call_count": 0,
    }


def test_closed_stage_is_excluded_entirely_and_at_risk_stage_lands_in_live_section(tmp_path):
    """Real bug reported live, 2026-09-21: "Remember, onboarding and
    development only. Closed accounts should be completely ignored for this
    purpose." Closed accounts were previously falling into the Not-live-yet
    section just because is_live (stage=="live" only) was False for them
    too -- same category of bug as the daily audit's At Risk/Closed
    exemption fix earlier in this session, just in a different pipeline."""
    client, session_factory = _client_and_session_factory(tmp_path)
    db = session_factory()
    accounts = [
        _stage_account("Onboarding Co", "onboarding", "needs_attention"),
        _stage_account("Development Co", "development", "at_risk"),
        _stage_account("At Risk Stage Co", "At Risk", "at_risk"),
        _stage_account("Closed Co", "closed", "on_track"),
        _stage_account("Live Co", "live", "on_track"),
    ]
    db.add(AtlasReportRun(
        run_at=datetime(2026, 9, 21, 9, 0, 0), limit_used=None,
        report_json=json.dumps({"count": len(accounts), "accounts": accounts, "narrative_batches": []}),
    ))
    db.commit()
    db.close()

    resp = client.get("/reports/atlas-account-status/pulse")

    assert resp.status_code == 200
    text = resp.text
    assert "Closed Co" not in text

    order = re.findall(r'data-company-name="([^"]+)"', text)
    # Only genuine pipeline stages in the Not-live-yet section (severity
    # sorted); At Risk-stage lands in Live (by elimination), not pipeline.
    assert order == ["Development Co", "Onboarding Co", "At Risk Stage Co", "Live Co"]
    assert text.index("Not live yet") < text.index('data-company-name="Development Co"')
    assert text.index('data-company-name="Onboarding Co"') < text.index(">Live<")
