"""
Tests the manual-trigger endpoint's response body directly (calling the
FastAPI route function itself, not over real HTTP/ASGI) — deliberately avoids
TestClient(app) here since app.main's lifespan starts a real BackgroundScheduler
singleton that isn't safe to start more than once per process across the test
suite. The route function *is* what FastAPI calls per request; calling it
directly still proves what actually reaches the JSON response body.

ATLAS-ONLY as of 2026-08-06: no more heartbeat/GoogleDriveClient fixture at
all — see app/tasks/daily_go_live_audit.py's module docstring.
"""
import time
from datetime import datetime, timedelta, timezone

import app.main as main_mod
import app.tasks.daily_go_live_audit as audit_mod
from app.db import get_engine, get_session_factory, init_db
from app.config import get_settings


def _wait_for_job(job_id: str, timeout: float = 5.0) -> dict:
    """Trigger runs on a real background thread (see app/tasks/job_tracker.py) --
    poll instead of assuming it's done by the time we check. The fakes below
    do no real I/O, so this finishes in well under a second in practice; the
    timeout is only a guard against a genuine regression hanging the test."""
    deadline = time.time() + timeout
    status_response = main_mod.get_daily_go_live_audit_run_status(job_id)
    while status_response["job_status"] == "running":
        if time.time() > deadline:
            raise TimeoutError(f"job {job_id} still running after {timeout}s")
        time.sleep(0.01)
        status_response = main_mod.get_daily_go_live_audit_run_status(job_id)
    return status_response


class _FakeAtlasClient:
    def get_all_accounts(self):
        created_at = (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        go_live = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return [
            {
                "id": "acme-co",
                "companyName": "Acme Co",
                "stage": "onboarding",
                "isActive": True,
                "createdAt": created_at,
                "deadlines": {"goLive": go_live},
                "integrations": {
                    "clickupFolderId": "folder1", "internalSlackChannelId": None,
                    "googleMccId": None, "metaAdAccountId": None,
                },
            }
        ]


class _FakeClickUp:
    def get_folder_lists(self, folder_id):
        if folder_id == "folder1":
            return [{"id": "list1", "name": "TODO"}]
        return []

    def get_list_tasks(self, list_id, include_closed=True, page=0):
        if list_id == "list1":
            recent = str(int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000))
            return {"tasks": [{"id": "card1", "name": "Acme onboarding", "date_updated": recent}], "last_page": True}
        return {"tasks": [], "last_page": True}

    def get_task_comments(self, task_id):
        if task_id != "card1":
            return []
        recent = str(int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000))
        return [{"comment_text": "waiting on client access", "date": recent}]


class _FakeGHL:
    def recent_closed_won(self, pipeline_id, stage_id, days=3):
        return []


class _FakeSlack:
    sent: list = []

    def send_dm(self, user_id, text):
        _FakeSlack.sent.append((user_id, text))
        return {}

    def join_all_public_channels(self):
        return {"joined": [], "already_in": [], "skipped_archived": [], "failed": []}

    def channel_history(self, channel_id, oldest_ts=None):
        return []


def test_trigger_endpoint_response_body_includes_gather_and_narrative_diagnostics(monkeypatch, tmp_path):
    db_path = tmp_path / "trigger.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    # GoogleAdsClient()/MetaAdsClient() construct unconditionally regardless
    # of whether any account has an ID set -- without these, adspend's
    # Settings() silently falls back to the real repo-root .env.
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_SECRET", "x")
    monkeypatch.setenv("GOOGLE_ADS_REFRESH_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "1234567890")
    monkeypatch.setenv("META_ACCESS_TOKEN", "x")
    from adspend.config import get_settings as get_adspend_settings
    get_adspend_settings.cache_clear()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(audit_mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(audit_mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(audit_mod, "GHLClient", _FakeGHL)
    monkeypatch.setattr(audit_mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(
        "app.tasks.dashboard_summary.synthesize_account_narratives",
        lambda accounts: (
            {},
            [{"batch_index": 0, "accounts": [a["account"] for a in accounts], "narrated_count": 0, "ok": True, "error": None}],
        ),
    )
    _FakeSlack.sent = []

    init_db()
    try:
        trigger_response = main_mod.trigger_daily_go_live_audit()
        assert trigger_response["job_status"] == "running"

        body = _wait_for_job(trigger_response["job_id"])
        assert body["job_status"] == "done"

        assert set(body.keys()) >= {"run_id", "status", "notes", "context_gather", "narrative_batches", "narrative_error"}

        # Per-account gather diagnostics for Acme Co (every Atlas account now, not just a package-based subset)
        acme_diagnostics = body["context_gather"]["Acme Co"]
        assert acme_diagnostics["clickup_ok"] is True
        assert acme_diagnostics["clickup_comment_count"] == 1
        assert acme_diagnostics["slack_ok"] is True
        assert acme_diagnostics["slack_channel_matched"] is None  # no slack_channel_id on this Atlas account
        assert acme_diagnostics["slack_match_confidence"] is None
        assert acme_diagnostics["google_ads_live_ok"] is None  # no googleMccId on this Atlas account
        assert acme_diagnostics["meta_ads_live_ok"] is None  # no metaAdAccountId on this Atlas account

        # Narrative batch outcome (the Claude call) is in the response body too
        assert len(body["narrative_batches"]) == 1
        assert body["narrative_batches"][0]["ok"] is True
        assert body["narrative_batches"][0]["accounts"] == ["Acme Co"]
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
