"""
Tests the manual-trigger endpoint's response body directly (calling the
FastAPI route function itself, not over real HTTP/ASGI) — deliberately avoids
TestClient(app) here since app.main's lifespan starts a real BackgroundScheduler
singleton that isn't safe to start more than once per process across the test
suite. The route function *is* what FastAPI calls per request; calling it
directly still proves what actually reaches the JSON response body.
"""
from datetime import datetime, timedelta, timezone

import app.main as main_mod
import app.tasks.daily_go_live_audit as audit_mod
from app.db import get_engine, get_session_factory, init_db
from app.config import get_settings
from app.integrations.google_drive import GoogleDriveClient as RealGoogleDriveClient


class _FakeGoogleDrive:
    is_stale = staticmethod(RealGoogleDriveClient.is_stale)

    _HEADER = [
        "Checked at", "Account name", "CID", "Enabled campaigns", "Enabled LSA",
        "Spend yesterday (ads)", "Spend yesterday (LSA)", "Spend today (ads)",
        "Spend today (LSA)", "AM-BUILD spend yest", "Legacy spend yest", "Status", "Flag",
    ]

    def read_sheet_values(self, file_id, tab):
        fresh = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        if tab == "Heartbeat":
            return [self._HEADER, [fresh, "Acme Co", "1", "2", "FALSE", "0", "0", "0", "0", "0", "0", "", ""]]
        return [self._HEADER]


class _FakeClickUp:
    def get_list_tasks(self, list_id, include_closed=True, page=0):
        fourteen_days_ago_ms = str(int((datetime.now(timezone.utc) - timedelta(days=15)).timestamp() * 1000))
        return {
            "tasks": [
                {
                    "id": "card1",
                    "name": "1) Onboarding · Acme Co · signed · [MKTG]",
                    "status": {"status": "onboarding"},
                    "tags": [{"name": "newclientgolivetracker"}],
                    "date_created": fourteen_days_ago_ms,
                }
            ],
            "last_page": True,
        }

    def get_task_with_subtasks(self, task_id):
        return {"id": task_id, "subtasks": []}

    def get_task_comments(self, task_id):
        return [{"comment_text": "waiting on client access"}] if task_id == "card1" else []


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

    def list_channels(self, types="public_channel,private_channel"):
        return []  # no channel match -- exercises the "slack_ok True, 0 messages" path

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
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(audit_mod, "GoogleDriveClient", _FakeGoogleDrive)
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
    db = get_session_factory()()
    try:
        body = main_mod.trigger_daily_go_live_audit(db=db)

        assert set(body.keys()) >= {"run_id", "status", "notes", "context_gather", "narrative_batches", "narrative_error"}

        # Per-account gather diagnostics for Acme Co (every matched account now, not just a package-based subset)
        acme_diagnostics = body["context_gather"]["Acme Co"]
        assert acme_diagnostics["clickup_ok"] is True
        assert acme_diagnostics["clickup_comment_count"] == 1
        assert acme_diagnostics["slack_ok"] is True
        assert acme_diagnostics["slack_channel_matched"] is None  # no channels returned by the fake
        assert acme_diagnostics["slack_match_confidence"] is None

        # Narrative batch outcome (the Claude call) is in the response body too
        assert len(body["narrative_batches"]) == 1
        assert body["narrative_batches"][0]["ok"] is True
        assert body["narrative_batches"][0]["accounts"] == ["Acme Co"]
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
