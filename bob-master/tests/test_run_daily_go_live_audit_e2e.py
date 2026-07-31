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


class _FakeGoogleDriveSpellingVariants:
    """Real bug from the first live run: the same client appears with different
    spelling/casing on each sheet (e.g. "vera plumbing and drain" on Meta vs
    "Vera Plumbing and Drain" on Google Ads) and got processed as two separate
    accounts. Google Ads side shows legacy-only spend; Meta side (different
    spelling) shows real AM-BUILD spend — cross-platform is_live only works if
    both rows resolve to the same canonical account."""

    is_stale = staticmethod(RealGoogleDriveClient.is_stale)

    def read_sheet_values(self, file_id, tab):
        fresh = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        if tab == "Heartbeat":
            # legacy-only on this platform, spelled with extra whitespace/casing
            return [_HEADER, [fresh, "vera  plumbing and drain", "1", "2", "FALSE", "0", "0", "0", "0", "0", "40", "", ""]]
        # Meta: real AM-BUILD spend, differently capitalized -> same client is live overall
        return [_HEADER, [fresh, "Vera Plumbing and Drain", "2", "1", "FALSE", "0", "0", "0", "0", "60", "0", "", ""]]


class _FakeClickUpNoMatch:
    def get_list_tasks(self, list_id, include_closed=True, page=0):
        return {"tasks": [], "last_page": True}


def test_spelling_variants_across_sheets_dedupe_to_one_account(monkeypatch, tmp_path):
    db_path = tmp_path / "dedupe.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDriveSpellingVariants)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUpNoMatch)
    monkeypatch.setattr(mod, "GHLClient", lambda: type("_G", (), {"recent_closed_won": lambda self, *a, **k: []})())
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        flags = db.query(mod.Flag).filter_by(run_id=run.id).all()
        heartbeat_flags = [f for f in flags if f.category == FlagCategory.heartbeat_mismatch]

        # Must NOT be flagged: the client is live via Meta (different spelling),
        # so the Google-side legacy-only spend shouldn't trigger a false alarm —
        # only possible if both spellings resolved to the same canonical account.
        assert heartbeat_flags == []
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


class _FakeGoogleDriveNumericAccountName:
    """Real examples from the Meta sheet, per GoLive_Audit_Dev_Handover_Brief.md
    §1: accounts never assigned a name in Meta Business Manager show up with a
    numeric-only "account name" — must not be silently fuzzy-matched."""

    is_stale = staticmethod(RealGoogleDriveClient.is_stale)

    def read_sheet_values(self, file_id, tab):
        fresh = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        if tab == "Heartbeat":
            return [_HEADER]
        return [_HEADER, [fresh, "106231623122110", "1", "2", "FALSE", "0", "0", "0", "0", "0", "40", "", ""]]


def test_numeric_only_account_name_flagged_not_matched(monkeypatch, tmp_path):
    db_path = tmp_path / "numeric.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDriveNumericAccountName)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUpNoMatch)
    monkeypatch.setattr(mod, "GHLClient", lambda: type("_G", (), {"recent_closed_won": lambda self, *a, **k: []})())
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        flags = db.query(mod.Flag).filter_by(run_id=run.id).all()

        # Flagged as needing name resolution, not silently dropped or fuzzy-matched
        unmapped = [f for f in flags if "unmapped account" in f.message]
        assert len(unmapped) == 1
        assert unmapped[0].client_name == "106231623122110"

        # And never treated as a real account for the legacy-only-spend check
        heartbeat_flags = [f for f in flags if f.category == FlagCategory.heartbeat_mismatch]
        assert heartbeat_flags == []
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


class _FakeGoogleDriveChurnedClient:
    """A client showing real AM-BUILD spend on the heartbeat — the exact
    scenario ACCURACY RULES §2 exists for: board/heartbeat says active, but
    the Retention pipeline says otherwise."""

    is_stale = staticmethod(RealGoogleDriveClient.is_stale)

    def read_sheet_values(self, file_id, tab):
        fresh = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        if tab == "Heartbeat":
            return [_HEADER, [fresh, "LG Electric", "1", "2", "FALSE", "0", "0", "0", "0", "500", "0", "", ""]]
        return [_HEADER]


class _FakeClickUpByList:
    """Branches on list_id so Go-Live and Retention can return different
    cards within the same test run, like the real board pull does."""

    def get_list_tasks(self, list_id, include_closed=True, page=0):
        if list_id == "retention-list":
            return {
                "tasks": [
                    {"id": "r1", "name": "[CHURNED] LG Electric", "status": {"status": "churned x"}, "tags": []},
                    {
                        "id": "r2",
                        "name": "📋 TEMPLATE — copy this for every request (do not close)",
                        "status": {"status": "new requests"},
                        "tags": [],
                    },
                ],
                "last_page": True,
            }
        return {"tasks": [], "last_page": True}  # no Go-Live match — isolates the retention check


def test_retention_churned_status_flags_despite_active_heartbeat_spend(monkeypatch, tmp_path):
    db_path = tmp_path / "retention.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("CLICKUP_RETENTION_LIST_ID", "retention-list")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDriveChurnedClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUpByList)
    monkeypatch.setattr(mod, "GHLClient", lambda: type("_G", (), {"recent_closed_won": lambda self, *a, **k: []})())
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        flags = db.query(mod.Flag).filter_by(run_id=run.id).all()

        churn_flags = [f for f in flags if "CHURNED" in f.message]
        assert len(churn_flags) == 1
        assert churn_flags[0].client_name == "LG Electric"
        assert churn_flags[0].severity.value == "urgent"
        assert churn_flags[0].evidence_url == "https://app.clickup.com/t/r1"

        # The template card must never surface as if it were a real client
        assert all("TEMPLATE" not in f.message for f in flags)
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


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
    _FakeSlack.sent = []

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
