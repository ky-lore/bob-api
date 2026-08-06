"""
End-to-end test of run_daily_go_live_audit() with the external clients faked
out, against a real (temp file) SQLite DB — proves the Atlas/heartbeat/ClickUp/
Slack wiring actually executes correctly at runtime, not just compiles.

Atlas-driven as of 2026-08-04: Atlas is the account universe (stage,
day-count from createdAt, exact clickupFolderId/internalSlackChannelId).
Fuzzy matching now only happens once, matching heartbeat sheet account names
against Atlas's clean companyName — not against ClickUp card titles anymore.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import app.tasks.daily_go_live_audit as mod
from app.config import get_settings
from app.db import get_engine, get_session_factory, init_db
from app.integrations.google_drive import GoogleDriveClient as RealGoogleDriveClient
from app.models import FlagCategory, RunStatus


@pytest.fixture(autouse=True)
def _mock_anthropic_narrative(monkeypatch):
    # Every test in this file needs ANTHROPIC_API_KEY set (now a required
    # setting) and must not make a real API call — dashboard_json generation
    # is exercised (stat tiles are real), just not the LLM synthesis step.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_account_narratives", lambda accounts: ({}, []))


_CAPTURED_NARRATIVE_ACCOUNTS: list = []


@pytest.fixture
def _capture_narrative_accounts(monkeypatch):
    """Swaps the narrative-synthesis mock for one that records what it was
    called with, so a test can assert on the rich context that actually
    reached the LLM input — the point of the full-context gather."""
    _CAPTURED_NARRATIVE_ACCOUNTS.clear()

    def _fake(accounts):
        _CAPTURED_NARRATIVE_ACCOUNTS.extend(accounts)
        return {}, []

    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_account_narratives", _fake)
    return _CAPTURED_NARRATIVE_ACCOUNTS


_HEADER = [
    "Checked at", "Account name", "CID", "Enabled campaigns", "Enabled LSA",
    "Spend yesterday (ads)", "Spend yesterday (LSA)", "Spend today (ads)",
    "Spend today (LSA)", "AM-BUILD spend yest", "Legacy spend yest", "Status", "Flag",
]


def _atlas_account(
    company_name,
    *,
    stage="onboarding",
    created_days_ago=15,
    clickup_folder_id=None,
    slack_channel_id=None,
    google_ads_customer_id=None,
    is_active=True,
    atlas_id=None,
):
    """Builds a minimal real-shaped Atlas account record (see
    app/integrations/atlas_client.py's docstring for the confirmed real
    schema). createdAt uses the "Z" suffix Atlas's real API actually sends."""
    created_at = (datetime.now(timezone.utc) - timedelta(days=created_days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "id": atlas_id or company_name.lower().replace(" ", "-"),
        "companyName": company_name,
        "stage": stage,
        "isActive": is_active,
        "createdAt": created_at,
        "integrations": {
            "clickupFolderId": clickup_folder_id,
            "internalSlackChannelId": slack_channel_id,
            "googleMccId": google_ads_customer_id,
        },
    }


class _FakeAtlasClient:
    """Class-attribute accounts list, reset per test (same pattern as
    _FakeSlack.sent) -- lets every test configure exactly the Atlas universe
    it needs without a separate subclass each time."""

    accounts: list = []

    def get_all_accounts(self):
        return _FakeAtlasClient.accounts


class _FakeGoogleAdsClient:
    """Class-attribute responses keyed by customer_id, reset per test (same
    pattern as _FakeAtlasClient). A customer_id with no entry raises, so a
    test can exercise the soft-fail path just by not registering one. Method
    name matches adspend.google_ads_client.GoogleAdsClient exactly -- this is
    imported and called in-process now, not through a separate HTTP client."""

    responses: dict = {}

    def get_account_spend(self, customer_id, date_range="LAST_7_DAYS"):
        if customer_id not in _FakeGoogleAdsClient.responses:
            raise RuntimeError(f"adspend error (fake): no customer {customer_id}")
        return _FakeGoogleAdsClient.responses[customer_id]


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
    """Folder-walk shape (Bob, 2026-08-04): get_folder_lists -> get_list_tasks
    -> get_task_comments, replacing the old card+subtasks shape entirely."""

    def get_folder_lists(self, folder_id):
        if folder_id == "folder1":
            return [{"id": "list1", "name": "TODO"}]
        return []

    def get_list_tasks(self, list_id, include_closed=True, page=0):
        if list_id == "list1":
            recent = str(int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000))
            return {"tasks": [{"id": "task1", "name": "Acme onboarding", "date_updated": recent}], "last_page": True}
        return {"tasks": [], "last_page": True}

    def get_task_comments(self, task_id):
        if task_id == "task1":
            recent = str(int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000))
            return [{"comment_text": "Client hasn't sent brand assets yet", "date": recent}]
        return []


class _FakeGHL:
    def recent_closed_won(self, pipeline_id, stage_id, days=3):
        return [{"name": "Brand New Client", "lastStageChangeAt": datetime.utcnow().isoformat() + "Z"}]


class _FakeSlack:
    sent: list = []

    def send_dm(self, user_id, text):
        _FakeSlack.sent.append((user_id, text))
        return {}

    def join_all_public_channels(self):
        return {"joined": [], "already_in": ["internal-acme-co"], "skipped_archived": [], "failed": []}

    def list_channels(self, types="public_channel,private_channel"):
        return [{"id": "C-ACME", "name": "internal-acme-co"}]

    def channel_history(self, channel_id, oldest_ts=None):
        if channel_id == "C-ACME":
            return [{"text": "launching soon, just waiting on assets"}]
        return []


def _no_ghl():
    return type("_G", (), {"recent_closed_won": lambda self, *a, **k: []})()


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


def test_spelling_variants_across_sheets_dedupe_to_one_account(monkeypatch, tmp_path):
    # Legacy-only-spend flagging happens in Step 1, entirely before Atlas
    # correlation -- an empty Atlas universe doesn't affect this assertion.
    db_path = tmp_path / "dedupe.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDriveSpellingVariants)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeAtlasClient.accounts = []
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
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDriveNumericAccountName)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeAtlasClient.accounts = []
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


class _FakeClickUpRetentionOnly:
    """Retention list returns real cards; every other list (Atlas folder
    walks, web-build sweep) is empty -- isolates the retention check."""

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
        return {"tasks": [], "last_page": True}

    def get_folder_lists(self, folder_id):
        return []

    def get_task_comments(self, task_id):
        return []


def test_retention_churned_status_flags_despite_active_heartbeat_spend(monkeypatch, tmp_path):
    db_path = tmp_path / "retention.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("CLICKUP_RETENTION_LIST_ID", "retention-list")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDriveChurnedClient)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUpRetentionOnly)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    # LG Electric must be in the Atlas universe -- retention check now
    # iterates Atlas company names, not raw heartbeat names.
    _FakeAtlasClient.accounts = [_atlas_account("LG Electric", stage="live")]
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
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _FakeGHL)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeAtlasClient.accounts = [
        _atlas_account("Acme Co", stage="onboarding", created_days_ago=15, clickup_folder_id="folder1", slack_channel_id="C-ACME")
    ]
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)

        assert run.status in (RunStatus.success, RunStatus.partial)

        flags = db.query(mod.Flag).filter_by(run_id=run.id).all()
        new_deal_flags = [f for f in flags if f.category == FlagCategory.new_deal]
        heartbeat_flags = [f for f in flags if f.category == FlagCategory.heartbeat_mismatch]

        # Acme Co: Atlas account, 15 days old (createdAt), zero heartbeat spend
        # -> not live -- shows up in accounts_overview regardless (fully macro).
        dashboard_data = json.loads(run.dashboard_json)
        by_account = {a["account"]: a for a in dashboard_data["accounts_overview"]}
        assert by_account["Acme Co"]["is_live"] is False
        assert by_account["Acme Co"]["day"] == 15

        # Zero spend everywhere means no legacy-only-spend flag (nothing to be legacy about)
        assert heartbeat_flags == []

        # GHL sweep still wired and producing a new-deal flag
        assert len(new_deal_flags) == 1
        assert new_deal_flags[0].client_name == "Brand New Client"

        # Slack DM was sent with the assembled digest -- Acme Co isn't in it
        # (no flag was raised for it; "not live yet" alone isn't alarm-worthy
        # without the old package clock), but the new-deal flag still is.
        assert len(_FakeSlack.sent) == 1
        assert "Brand New Client" in _FakeSlack.sent[0][1]
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


class _FakeSlackJoinsNewChannels(_FakeSlack):
    """Reports actually joining a new public channel -- verifies the
    auto-join step runs and surfaces its result."""

    def join_all_public_channels(self):
        return {
            "joined": ["internal-fresh-signup"],
            "already_in": ["internal-acme-co"],
            "skipped_archived": ["old-project-x", "old-project-y"],
            "failed": [],
        }


def test_slack_auto_join_runs_before_context_gather_and_is_noted(monkeypatch, tmp_path):
    db_path = tmp_path / "autojoin.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _FakeGHL)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlackJoinsNewChannels)
    _FakeAtlasClient.accounts = []
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        assert "1 newly joined" in run.notes
        assert "2 archived (skipped)" in run.notes
        assert "internal-fresh-signup" not in run.notes  # only failures get named, not successes
        assert "old-project-x" not in run.notes  # archived channels don't get named either
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


class _FakeSlackManyJoinFailures(_FakeSlack):
    """Real scenario: a live run against ~750 channels produced hundreds of
    failure lines before archived channels were excluded and a cap was added
    -- this reproduces that shape at a testable scale to prove run.notes
    stays readable."""

    def join_all_public_channels(self):
        return {
            "joined": [],
            "already_in": [],
            "skipped_archived": [f"archived-{i}" for i in range(500)],
            "failed": [f"channel-{i}: ratelimited" for i in range(8)],
        }


def test_slack_auto_join_caps_failure_detail_in_notes(monkeypatch, tmp_path):
    db_path = tmp_path / "autojoin_many_failures.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _FakeGHL)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlackManyJoinFailures)
    _FakeAtlasClient.accounts = []
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        assert "500 archived (skipped)" in run.notes
        assert "8 failed" in run.notes
        assert "channel-0: ratelimited" in run.notes
        assert "+3 more" in run.notes  # 8 failures, only first 5 named
        assert "channel-7: ratelimited" not in run.notes
        assert len(run.notes) < 2000  # nowhere near the ~50k-char blob this used to produce
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


class _FakeSlackJoinFails(_FakeSlack):
    def join_all_public_channels(self):
        raise RuntimeError("missing_scope: channels:join")


def test_slack_auto_join_failure_does_not_crash_the_run(monkeypatch, tmp_path):
    db_path = tmp_path / "autojoin_fail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _FakeGHL)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlackJoinFails)
    _FakeAtlasClient.accounts = []
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        assert run.status in (RunStatus.success, RunStatus.partial)
        assert "missing_scope" in run.notes
        # The rest of the run (context gather, dashboard, digest) still completes.
        assert run.dashboard_json is not None
        assert len(_FakeSlack.sent) == 1
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_full_context_gather_reaches_the_narrative_llm_input(monkeypatch, tmp_path, _capture_narrative_accounts):
    """Proves the full-context flow end-to-end: ClickUp folder comments and
    the Atlas-exact-ID Slack channel's full history both actually reach the
    LLM's input for a real matched account."""
    db_path = tmp_path / "rich_context.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _FakeGHL)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeAtlasClient.accounts = [
        _atlas_account("Acme Co", stage="onboarding", created_days_ago=15, clickup_folder_id="folder1", slack_channel_id="C-ACME")
    ]
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        mod.run_daily_go_live_audit(db)

        assert len(_capture_narrative_accounts) == 1
        acme = _capture_narrative_accounts[0]
        assert acme["account"] == "Acme Co"
        assert any("Client hasn't sent brand assets yet" in c for c in acme["context"])
        assert any("launching soon, just waiting on assets" in c for c in acme["context"])
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


class _FakeClickUpWithWebBuilds:
    """Web-build list returns two real-shaped cards -- one very stale (the
    ColdRiite-style case), one fresh. Every other list (Atlas folder walks)
    is empty -- isolates the web-build sweep."""

    def get_list_tasks(self, list_id, include_closed=True, page=0):
        if list_id == "web-build-list":
            long_ago_ms = str(int((datetime.now(timezone.utc) - timedelta(days=374)).timestamp() * 1000))
            recent_ms = str(int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp() * 1000))
            return {
                "tasks": [
                    {"id": "wb1", "name": "ColdRiite Walk-Ins", "status": {"status": "development"}, "date_created": long_ago_ms},
                    {"id": "wb2", "name": "Fresh Site Co", "status": {"status": "in progress"}, "date_created": recent_ms},
                ],
                "last_page": True,
            }
        return {"tasks": [], "last_page": True}

    def get_folder_lists(self, folder_id):
        return []

    def get_task_comments(self, task_id):
        return []


def test_web_build_pipeline_sweep_pulls_stale_builds_into_the_dashboard(monkeypatch, tmp_path):
    db_path = tmp_path / "webbuild.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("CLICKUP_WEB_BUILD_LIST_ID", "web-build-list")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUpWithWebBuilds)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeAtlasClient.accounts = []
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)

        # oldest first, no >30-day threshold filtering -- both show up, macro/visibility only
        assert [b["name"] for b in dashboard_data["web_builds"]] == ["ColdRiite Walk-Ins", "Fresh Site Co"]
        coldriite = dashboard_data["web_builds"][0]
        assert coldriite["day"] == 374
        assert coldriite["status"] == "development"
        assert coldriite["card_id"] == "wb1"
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


class _FakeClickUpWebBuildPullFails:
    def get_list_tasks(self, list_id, include_closed=True, page=0):
        if list_id == "web-build-list":
            raise RuntimeError("ClickUp API 503")
        return {"tasks": [], "last_page": True}

    def get_folder_lists(self, folder_id):
        return []

    def get_task_comments(self, task_id):
        return []


def test_web_build_pipeline_pull_failure_does_not_crash_the_run(monkeypatch, tmp_path):
    db_path = tmp_path / "webbuild_fail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("CLICKUP_WEB_BUILD_LIST_ID", "web-build-list")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUpWebBuildPullFails)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeAtlasClient.accounts = []
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        assert run.status in (RunStatus.success, RunStatus.partial)
        assert "Web Build Pipeline pull failed" in run.notes
        dashboard_data = json.loads(run.dashboard_json)
        assert dashboard_data["web_builds"] == []
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


class _FakeAtlasClientFails:
    def get_all_accounts(self):
        raise RuntimeError("Atlas API 503")


def test_atlas_pull_failure_does_not_crash_the_run(monkeypatch, tmp_path):
    db_path = tmp_path / "atlas_fail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClientFails)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        assert run.status in (RunStatus.success, RunStatus.partial)
        assert "Atlas accounts pull failed" in run.notes
        dashboard_data = json.loads(run.dashboard_json)
        assert dashboard_data["accounts_overview"] == []
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_heartbeat_account_with_no_atlas_match_is_flagged_not_silently_dropped(monkeypatch, tmp_path):
    """Closes the old TODO: heartbeat data for an account Atlas doesn't know
    about used to be silently skipped. Now it's flagged instead."""
    db_path = tmp_path / "no_atlas_match.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    # Atlas has zero accounts at all -- "Acme Co" (from the heartbeat sheet)
    # cannot match anything.
    _FakeAtlasClient.accounts = []
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        flags = db.query(mod.Flag).filter_by(run_id=run.id).all()

        unmatched = [f for f in flags if "doesn't match any active Atlas client" in f.message]
        assert len(unmatched) == 1
        assert unmatched[0].client_name == "Acme Co"
        assert unmatched[0].unverified is True
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_debug_max_accounts_caps_the_atlas_universe_deterministically(monkeypatch, tmp_path):
    db_path = tmp_path / "debug_cap.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    monkeypatch.setenv("DEBUG_MAX_ACCOUNTS", "2")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    # 4 accounts in Atlas, capped to 2 -- sorted by companyName: Alpha, Beta kept.
    _FakeAtlasClient.accounts = [
        _atlas_account("Delta Co"),
        _atlas_account("Beta Co"),
        _atlas_account("Alpha Co"),
        _atlas_account("Charlie Co"),
    ]
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)

        accounts = {a["account"] for a in dashboard_data["accounts_overview"]}
        assert accounts == {"Alpha Co", "Beta Co"}
        assert "DEBUG: capped to 2" in run.notes
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_debug_max_accounts_none_means_no_cap(monkeypatch, tmp_path):
    db_path = tmp_path / "no_cap.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeAtlasClient.accounts = [_atlas_account("Delta Co"), _atlas_account("Beta Co"), _atlas_account("Alpha Co")]
    _FakeSlack.sent = []

    from app.config import Settings
    assert Settings.model_fields["debug_max_accounts"].default == 5  # sanity check on the real default

    init_db()
    db = get_session_factory()()
    try:
        # Settings default (5) exceeds our 3 fake accounts, so nothing gets capped.
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)
        accounts = {a["account"] for a in dashboard_data["accounts_overview"]}
        assert accounts == {"Delta Co", "Beta Co", "Alpha Co"}
        assert "DEBUG: capped" not in (run.notes or "")
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_live_google_ads_spend_is_blended_in_alongside_heartbeat_spend(monkeypatch, tmp_path):
    db_path = tmp_path / "adspend_blend.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    _FakeAtlasClient.accounts = [_atlas_account("Acme Co", google_ads_customer_id="1234567890")]
    _FakeGoogleAdsClient.responses = {
        "1234567890": {
            "total_cost": 42.5,
            "total_impressions": 900,
            "total_clicks": 40,
            "total_conversions": 3.0,
            "enabled_campaign_count": 1,
            "campaigns": [
                {"id": "1", "name": "Search", "status": "ENABLED", "channel_type": "SEARCH", "cost": 42.5,
                 "impressions": 900, "clicks": 40, "ctr": 0.044, "avg_cpc": 1.06, "conversions": 3.0,
                 "cost_per_conversion": 14.17, "conversions_value": 3.0},
                {"id": "2", "name": "Old Display", "status": "REMOVED", "channel_type": "DISPLAY", "cost": 0.0,
                 "impressions": 0, "clicks": 0, "ctr": 0.0, "avg_cpc": 0.0, "conversions": 0.0,
                 "cost_per_conversion": 0.0, "conversions_value": 0.0},
            ],
        },
    }
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)

        acme = next(a for a in dashboard_data["accounts_overview"] if a["account"] == "Acme Co")
        live_spend = acme["ad_spend"]["Google Ads (live)"]
        assert live_spend["spend"] == 42.5
        assert live_spend["enabled_campaigns"] == 1
        assert live_spend["impressions"] == 900
        assert live_spend["clicks"] == 40
        assert live_spend["conversions"] == 3.0
        # Full list, including the REMOVED one -- this is the internal
        # dashboard, not the Atlas export's enabled-only compressed cut.
        assert len(live_spend["campaigns"]) == 2
        assert live_spend["campaigns"][0]["name"] == "Search"
        assert live_spend["campaigns"][1]["status"] == "REMOVED"

        diagnostics = json.loads(run.context_gather_json)
        assert diagnostics["Acme Co"]["google_ads_live_ok"] is True
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_live_google_ads_spend_failure_is_soft_failed_not_run_crashing(monkeypatch, tmp_path):
    db_path = tmp_path / "adspend_fail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "GoogleDriveClient", _FakeGoogleDrive)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    # "9999999999" has no entry in _FakeGoogleAdsClient.responses -- forces the error path.
    _FakeAtlasClient.accounts = [_atlas_account("Acme Co", google_ads_customer_id="9999999999")]
    _FakeGoogleAdsClient.responses = {}
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)

        acme = next(a for a in dashboard_data["accounts_overview"] if a["account"] == "Acme Co")
        assert "Google Ads (live)" not in acme["ad_spend"]

        diagnostics = json.loads(run.context_gather_json)
        assert diagnostics["Acme Co"]["google_ads_live_ok"] is False
        assert "no customer 9999999999" in diagnostics["Acme Co"]["google_ads_live_error"]
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
