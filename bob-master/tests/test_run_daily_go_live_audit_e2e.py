"""
End-to-end test of run_daily_go_live_audit() with the four external clients
faked out, against a real (temp file) SQLite DB — proves the ClickUp/matching/
package-clock wiring actually executes correctly at runtime, not just compiles.
"""
from datetime import datetime, timedelta, timezone

import app.tasks.daily_go_live_audit as mod
from app.config import get_settings
from app.db import get_engine, get_session_factory, init_db
from app.integrations.google_drive import GoogleDriveClient as RealGoogleDriveClient
from app.models import FlagCategory, RunStatus

_HEADER = [
    "Checked at", "Account name", "CID", "Enabled campaigns", "Enabled LSA",
    "Spend yesterday (ads)", "Spend yesterday (LSA)", "Spend today (ads)",
    "Spend today (LSA)", "AM-BUILD spend yest", "Legacy spend yest", "Status", "Flag",
]


class _FakeGoogleDrive:
    # Delegates to the real is_stale — re-validates real freshness logic too,
    # since it's called directly on the class (GoogleDriveClient.is_stale), not
    # on an instance, so this needs to be swappable as a whole class.
    is_stale = staticmethod(RealGoogleDriveClient.is_stale)

    def read_sheet_values(self, file_id, tab):
        fresh = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        if tab == "Heartbeat":
            # Acme Co: zero spend everywhere -> not live, no legacy-only flag either.
            return [_HEADER, [fresh, "Acme Co", "1", "2", "FALSE", "0", "0", "0", "0", "0", "0", "", ""]]
        return [_HEADER]  # Meta sheet: header only, no accounts


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


class _FakeGHL:
    def recent_closed_won(self, pipeline_id, stage_id, days=3):
        return [{"name": "Brand New Client", "lastStageChangeAt": datetime.utcnow().isoformat() + "Z"}]


class _FakeSlack:
    sent: list = []

    def send_dm(self, user_id, text):
        _FakeSlack.sent.append((user_id, text))
        return {}


def test_run_daily_go_live_audit_end_to_end(monkeypatch, tmp_path):
    db_path = tmp_path / "e2e.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _FakeGHL)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)

        assert run.status in (RunStatus.success, RunStatus.partial)

        flags = db.query(mod.Flag).filter_by(run_id=run.id).all()
        clock_flags = [f for f in flags if f.category == FlagCategory.clock_violation]
        new_deal_flags = [f for f in flags if f.category == FlagCategory.new_deal]
        heartbeat_flags = [f for f in flags if f.category == FlagCategory.heartbeat_mismatch]

        # Acme Co: matched to card1 (pkg-mktg, ~15 days old, not live) -> Day 14 flag
        assert len(clock_flags) == 1
        assert clock_flags[0].client_name == "Acme Co"
        assert "Day 14" in clock_flags[0].message
        assert clock_flags[0].evidence_url == "https://app.clickup.com/t/card1"

        # Zero spend everywhere means no legacy-only-spend flag (nothing to be legacy about)
        assert heartbeat_flags == []

        # GHL sweep still wired and producing a new-deal flag
        assert len(new_deal_flags) == 1
        assert new_deal_flags[0].client_name == "Brand New Client"

        # Slack DM was sent with the assembled digest
        assert len(_FakeSlack.sent) == 1
        assert "Acme Co" in _FakeSlack.sent[0][1]
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
