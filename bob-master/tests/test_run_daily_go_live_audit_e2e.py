"""
End-to-end test of run_daily_go_live_audit() with the external clients faked
out, against a real (temp file) SQLite DB — proves the Atlas/ClickUp/Slack/
Google-Ads wiring actually executes correctly at runtime, not just compiles.

ATLAS-ONLY as of 2026-08-06: the heartbeat Google Sheets pull and the
retention-pipeline cross-check are both gone entirely (Bob: "drop the
heartbeat sheet from any sort of calculations or logic... that shit's
broken" / "assume Atlas will always have perfect data regarding account
status"). Atlas is the sole account universe; is_live comes only from real
Google Ads spend; the go-live target clock compares against Atlas's own
deadlines.goLive, not a recomputed uniform 14 days.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import app.tasks.daily_go_live_audit as mod
from app.config import get_settings
from app.db import get_engine, get_session_factory, init_db
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


def _atlas_account(
    company_name,
    *,
    stage="onboarding",
    created_days_ago=15,
    go_live_days_from_now=30,  # far in the future by default -> "on_track", doesn't affect unrelated tests
    clickup_folder_id=None,
    slack_channel_id=None,
    google_ads_customer_id=None,
    meta_ad_account_id=None,
    is_active=True,
    atlas_id=None,
):
    """Builds a minimal real-shaped Atlas account record (see
    app/integrations/atlas_client.py's docstring for the confirmed real
    schema). createdAt/goLive use the "Z" suffix Atlas's real API actually
    sends. go_live_days_from_now=None omits deadlines.goLive entirely (the
    "unknown" target_status case)."""
    created_at = (datetime.now(timezone.utc) - timedelta(days=created_days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    deadlines = {}
    if go_live_days_from_now is not None:
        go_live = (datetime.now(timezone.utc) + timedelta(days=go_live_days_from_now)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        deadlines = {"goLive": go_live}
    return {
        "id": atlas_id or company_name.lower().replace(" ", "-"),
        "companyName": company_name,
        "stage": stage,
        "isActive": is_active,
        "createdAt": created_at,
        "deadlines": deadlines,
        "integrations": {
            "clickupFolderId": clickup_folder_id,
            "internalSlackChannelId": slack_channel_id,
            "googleMccId": google_ads_customer_id,
            "metaAdAccountId": meta_ad_account_id,
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
    test can exercise the soft-fail path just by not registering one."""

    responses: dict = {}

    def get_account_spend(self, customer_id, date_range="LAST_7_DAYS"):
        if customer_id not in _FakeGoogleAdsClient.responses:
            raise RuntimeError(f"adspend error (fake): no customer {customer_id}")
        return _FakeGoogleAdsClient.responses[customer_id]


class _FakeMetaAdsClient:
    """Same class-attribute pattern as _FakeGoogleAdsClient, keyed by
    ad_account_id."""

    responses: dict = {}

    def get_account_spend(self, ad_account_id, date_range="LAST_7_DAYS"):
        if ad_account_id not in _FakeMetaAdsClient.responses:
            raise RuntimeError(f"adspend error (fake): no ad account {ad_account_id}")
        return _FakeMetaAdsClient.responses[ad_account_id]


def _spend(total_cost=0.0, enabled_campaign_count=0, campaigns=None):
    return {
        "total_cost": total_cost,
        "total_impressions": 0,
        "total_clicks": 0,
        "total_conversions": 0.0,
        "enabled_campaign_count": enabled_campaign_count,
        "campaigns": campaigns or [],
    }


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


def _setenv_common(monkeypatch):
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    # GoogleAdsClient()/MetaAdsClient() are constructed unconditionally at the
    # top of the gather loop regardless of whether any account actually has a
    # customer_id/ad_account_id -- without these, adspend.config.Settings()
    # silently falls back to reading the real repo-root .env (real local
    # credentials), which happened to mask this gap until it was caught
    # 2026-08-06 while wiring in Meta.
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_SECRET", "x")
    monkeypatch.setenv("GOOGLE_ADS_REFRESH_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "1234567890")
    monkeypatch.setenv("META_ACCESS_TOKEN", "x")
    from adspend.config import get_settings as get_adspend_settings
    get_adspend_settings.cache_clear()


def test_run_daily_go_live_audit_end_to_end(monkeypatch, tmp_path):
    db_path = tmp_path / "e2e.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

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

        # Acme Co: Atlas account, 15 days old (createdAt), no googleMccId ->
        # no confirmed spend -> not live -- shows up in accounts_overview
        # regardless (fully macro).
        dashboard_data = json.loads(run.dashboard_json)
        by_account = {a["account"]: a for a in dashboard_data["accounts_overview"]}
        assert by_account["Acme Co"]["is_live"] is False
        assert by_account["Acme Co"]["day"] == 15

        # GHL sweep still wired and producing a new-deal flag
        assert len(new_deal_flags) == 1
        assert new_deal_flags[0].client_name == "Brand New Client"

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
    _setenv_common(monkeypatch)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

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
    _setenv_common(monkeypatch)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

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
    _setenv_common(monkeypatch)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

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
    _setenv_common(monkeypatch)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

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
    _setenv_common(monkeypatch)
    monkeypatch.setenv("CLICKUP_WEB_BUILD_LIST_ID", "web-build-list")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

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
    _setenv_common(monkeypatch)
    monkeypatch.setenv("CLICKUP_WEB_BUILD_LIST_ID", "web-build-list")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

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
    _setenv_common(monkeypatch)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

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


def test_debug_max_accounts_caps_the_atlas_universe_to_a_random_subset(monkeypatch, tmp_path):
    db_path = tmp_path / "debug_cap.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.setenv("DEBUG_MAX_ACCOUNTS", "2")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    all_names = {"Delta Co", "Beta Co", "Alpha Co", "Charlie Co"}
    _FakeAtlasClient.accounts = [_atlas_account(n) for n in all_names]
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)

        # Random, not sorted-first-N -- assert count + membership, not which two.
        accounts = {a["account"] for a in dashboard_data["accounts_overview"]}
        assert len(accounts) == 2
        assert accounts.issubset(all_names)
        assert "DEBUG: capped to a random 2" in run.notes
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_debug_max_accounts_none_means_no_cap(monkeypatch, tmp_path):
    db_path = tmp_path / "no_cap.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeAtlasClient.accounts = [_atlas_account("Delta Co"), _atlas_account("Beta Co"), _atlas_account("Alpha Co")]
    _FakeSlack.sent = []

    from app.config import Settings
    assert Settings.model_fields["debug_max_accounts"].default == 20  # sanity check on the real default

    init_db()
    db = get_session_factory()()
    try:
        # Settings default (20) exceeds our 3 fake accounts, so nothing gets capped.
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


def test_real_google_ads_spend_is_informational_and_does_not_gate_is_live(monkeypatch, tmp_path):
    db_path = tmp_path / "adspend_blend.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    # is_live now comes from Atlas's stage, not this spend -- stage="live" here.
    _FakeAtlasClient.accounts = [_atlas_account("Acme Co", stage="live", google_ads_customer_id="1234567890")]
    _FakeGoogleAdsClient.responses = {
        "1234567890": _spend(
            total_cost=42.5,
            enabled_campaign_count=1,
            campaigns=[
                {"id": "1", "name": "Search", "status": "ENABLED", "channel_type": "SEARCH", "cost": 42.5,
                 "impressions": 900, "clicks": 40, "ctr": 0.044, "avg_cpc": 1.06, "conversions": 3.0,
                 "cost_per_conversion": 14.17, "conversions_value": 3.0},
                # Paused mid-window but still spent money -- "recently changed"
                # proxy (see filter_relevant_campaigns), should survive the filter.
                {"id": "2", "name": "Paused Mid-Window", "status": "PAUSED", "channel_type": "SEARCH", "cost": 5.0,
                 "impressions": 50, "clicks": 2, "ctr": 0.04, "avg_cpc": 2.5, "conversions": 0.0,
                 "cost_per_conversion": 0.0, "conversions_value": 0.0},
                # Long-dead, zero activity in the window -- should be dropped.
                {"id": "3", "name": "Old Display", "status": "REMOVED", "channel_type": "DISPLAY", "cost": 0.0,
                 "impressions": 0, "clicks": 0, "ctr": 0.0, "avg_cpc": 0.0, "conversions": 0.0,
                 "cost_per_conversion": 0.0, "conversions_value": 0.0},
            ],
        ),
    }
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)

        acme = next(a for a in dashboard_data["accounts_overview"] if a["account"] == "Acme Co")
        assert acme["is_live"] is True  # from Atlas stage=="live", not from this spend
        assert acme["target_status"] == "live"
        live_spend = acme["ad_spend"]["Google Ads"]
        assert live_spend["spend"] == 42.5
        assert live_spend["enabled_campaigns"] == 1
        # Live + recently-active only -- the long-dead REMOVED campaign with
        # zero activity in the window is dropped (see filter_relevant_campaigns).
        campaign_names = {c["name"] for c in live_spend["campaigns"]}
        assert campaign_names == {"Search", "Paused Mid-Window"}
        assert "Old Display" not in campaign_names

        diagnostics = json.loads(run.context_gather_json)
        assert diagnostics["Acme Co"]["google_ads_live_ok"] is True
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_google_ads_pull_failure_leaves_account_not_live_and_soft_fails(monkeypatch, tmp_path):
    db_path = tmp_path / "adspend_fail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

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
        assert "Google Ads" not in acme["ad_spend"]
        assert acme["is_live"] is False  # no confirmed spend -- can't be live
        # Has a real CID on file, pull failed -- a real problem, distinct from
        # "no service" (2026-08-10, see dashboard_summary.py's ad_platform_errors).
        assert "no customer 9999999999" in acme["ad_spend_errors"]["Google Ads"]

        diagnostics = json.loads(run.context_gather_json)
        assert diagnostics["Acme Co"]["google_ads_live_ok"] is False
        assert "no customer 9999999999" in diagnostics["Acme Co"]["google_ads_live_error"]
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_account_with_no_platform_ids_at_all_has_no_spend_and_no_errors(monkeypatch, tmp_path):
    """Confirms the 'no service' case flows through cleanly and distinctly
    from a real pull failure -- see test_google_ads_pull_failure_leaves_
    account_not_live_and_soft_fails for the contrasting case."""
    db_path = tmp_path / "no_service.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeAtlasClient.accounts = [_atlas_account("Website Only Co", stage="live")]
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)

        acme = next(a for a in dashboard_data["accounts_overview"] if a["account"] == "Website Only Co")
        assert acme["ad_spend"] == {}
        assert acme["ad_spend_errors"] == {}
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_go_live_target_status_behind_when_past_atlas_deadline(monkeypatch, tmp_path):
    db_path = tmp_path / "behind.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    # No googleMccId -> not live -> target status is evaluated against the
    # (already-past) Atlas deadline.
    _FakeAtlasClient.accounts = [_atlas_account("Behind Co", stage="onboarding", go_live_days_from_now=-5)]
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)

        behind_co = next(a for a in dashboard_data["accounts_overview"] if a["account"] == "Behind Co")
        assert behind_co["target_status"] == "behind"
        assert dashboard_data["stat_tiles"]["behind"] == 1

        chart_row = next(r for r in dashboard_data["accounts_chart"] if r["account"] == "Behind Co")
        assert chart_row["target_status"] == "behind"
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_go_live_target_status_on_track_when_deadline_far_out(monkeypatch, tmp_path):
    db_path = tmp_path / "on_track.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    _FakeAtlasClient.accounts = [_atlas_account("Fresh Co", go_live_days_from_now=30)]
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)

        fresh_co = next(a for a in dashboard_data["accounts_overview"] if a["account"] == "Fresh Co")
        assert fresh_co["target_status"] == "on_track"
        assert dashboard_data["stat_tiles"]["behind"] == 0
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_should_be_on_but_dark_flag_from_real_atlas_stage_and_zero_spend(monkeypatch, tmp_path):
    db_path = tmp_path / "should_be_on.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    _FakeAtlasClient.accounts = [_atlas_account("Dark Co", stage="live", google_ads_customer_id="1112223333")]
    _FakeGoogleAdsClient.responses = {"1112223333": _spend(total_cost=0.0, enabled_campaign_count=1)}
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        flags = db.query(mod.Flag).filter_by(run_id=run.id).all()

        should_be_on = [f for f in flags if f.category == FlagCategory.ads_off_should_be_on]
        zero_spend = [f for f in flags if f.category == FlagCategory.ads_off_zero_spend]
        assert should_be_on and should_be_on[0].client_name == "Dark Co"
        assert zero_spend and zero_spend[0].client_name == "Dark Co"
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_verified_off_flag_trusts_atlas_stage_closed_directly(monkeypatch, tmp_path):
    """No cross-check against ad-platform data at all -- 'assume Atlas will
    always have perfect data regarding account status' (Bob, 2026-08-06)."""
    db_path = tmp_path / "verified_off.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    # No googleMccId at all -- verified_off must not depend on having spend data.
    _FakeAtlasClient.accounts = [_atlas_account("Closed Co", stage="closed")]
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        flags = db.query(mod.Flag).filter_by(run_id=run.id).all()

        verified_off = [f for f in flags if f.category == FlagCategory.ads_off_verified_off]
        assert verified_off and verified_off[0].client_name == "Closed Co"
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_billing_unsettled_flag_is_never_generated_placeholder(monkeypatch, tmp_path):
    """Bob, 2026-08-06: 'have billing placeholder for now... that'll take a
    while to reconcile' -- no scenario should ever produce this flag yet."""
    db_path = tmp_path / "billing_placeholder.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    _FakeAtlasClient.accounts = [_atlas_account("Dark Co", stage="live", google_ads_customer_id="1112223333")]
    _FakeGoogleAdsClient.responses = {"1112223333": _spend(total_cost=0.0, enabled_campaign_count=1)}
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        flags = db.query(mod.Flag).filter_by(run_id=run.id).all()
        assert [f for f in flags if f.category == FlagCategory.ads_off_unsettled] == []
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_go_live_target_status_unit_approaching_within_buffer():
    deadline = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert mod._go_live_target_status(deadline, is_live=False) == "approaching"


def test_go_live_target_status_unit_on_track_beyond_buffer():
    deadline = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert mod._go_live_target_status(deadline, is_live=False) == "on_track"


def test_go_live_target_status_unit_behind_when_past():
    deadline = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert mod._go_live_target_status(deadline, is_live=False) == "behind"


def test_go_live_target_status_unit_live_overrides_deadline_entirely():
    # Even a long-past deadline doesn't matter once real spend confirms live.
    deadline = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert mod._go_live_target_status(deadline, is_live=True) == "live"


def test_go_live_target_status_unit_unknown_when_no_deadline_on_file():
    assert mod._go_live_target_status(None, is_live=False) == "unknown"


def test_go_live_target_status_unit_unknown_on_unparseable_deadline():
    assert mod._go_live_target_status("not-a-date", is_live=False) == "unknown"


def test_is_live_comes_from_atlas_stage_not_spend_in_either_direction(monkeypatch, tmp_path):
    """Bob, 2026-08-06: 'not all clients NEED meta or google spend to be
    considered live... I want to use Atlas as the source of truth for is
    live or not.' Real spend must never override Atlas's stage either way:
    an onboarding account with real spend is still not live; a live-stage
    account with zero spend (e.g. website-only, or between campaigns) is
    still live."""
    db_path = tmp_path / "is_live_from_stage.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    _FakeAtlasClient.accounts = [
        _atlas_account("Onboarding With Spend", stage="onboarding", google_ads_customer_id="1111111111"),
        _atlas_account("Live No Ads", stage="live", google_ads_customer_id=None),
    ]
    _FakeGoogleAdsClient.responses = {
        "1111111111": _spend(total_cost=500.0, enabled_campaign_count=2),
    }
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)
        by_account = {a["account"]: a for a in dashboard_data["accounts_overview"]}

        # Real spend ($500!) does not make an onboarding-stage account live.
        assert by_account["Onboarding With Spend"]["is_live"] is False

        # No ad spend at all (website-only) does not make a live-stage account not-live.
        assert by_account["Live No Ads"]["is_live"] is True
        assert by_account["Live No Ads"]["target_status"] == "live"
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_meta_spend_blends_alongside_google_ads_in_spend_by_account(monkeypatch, tmp_path):
    db_path = tmp_path / "meta_blend.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    monkeypatch.setattr(mod, "MetaAdsClient", _FakeMetaAdsClient)
    _FakeAtlasClient.accounts = [
        _atlas_account("Acme Co", stage="live", google_ads_customer_id="1234567890", meta_ad_account_id="act_555")
    ]
    _FakeGoogleAdsClient.responses = {"1234567890": _spend(total_cost=42.5, enabled_campaign_count=1)}
    _FakeMetaAdsClient.responses = {"act_555": _spend(total_cost=17.0, enabled_campaign_count=2)}
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)

        acme = next(a for a in dashboard_data["accounts_overview"] if a["account"] == "Acme Co")
        assert acme["ad_spend"]["Google Ads"]["spend"] == 42.5
        assert acme["ad_spend"]["Meta"]["spend"] == 17.0

        diagnostics = json.loads(run.context_gather_json)
        assert diagnostics["Acme Co"]["meta_ads_live_ok"] is True
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_meta_pull_failure_is_soft_failed_google_ads_still_shows(monkeypatch, tmp_path):
    db_path = tmp_path / "meta_fail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    monkeypatch.setattr(mod, "MetaAdsClient", _FakeMetaAdsClient)
    _FakeAtlasClient.accounts = [
        _atlas_account("Acme Co", stage="live", google_ads_customer_id="1234567890", meta_ad_account_id="act_not_covered")
    ]
    _FakeGoogleAdsClient.responses = {"1234567890": _spend(total_cost=42.5, enabled_campaign_count=1)}
    _FakeMetaAdsClient.responses = {}  # act_not_covered has no entry -- forces the error path
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json)

        acme = next(a for a in dashboard_data["accounts_overview"] if a["account"] == "Acme Co")
        assert acme["ad_spend"]["Google Ads"]["spend"] == 42.5
        assert "Meta" not in acme["ad_spend"]
        # Real ACT ID on file, pull failed -- flagged distinctly, not "no service".
        assert "act_not_covered" in acme["ad_spend_errors"]["Meta"]
        assert "Google Ads" not in acme["ad_spend_errors"]  # Google succeeded, no error for it

        diagnostics = json.loads(run.context_gather_json)
        assert diagnostics["Acme Co"]["meta_ads_live_ok"] is False
        assert "act_not_covered" in diagnostics["Acme Co"]["meta_ads_live_error"]
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


def test_should_be_on_but_dark_not_flagged_when_live_via_meta_end_to_end(monkeypatch, tmp_path):
    """Integration-level version of the same rule already unit-tested in
    test_ads_off_classification.py: a client spending real money on Meta must
    not get flagged just because Google is dark."""
    db_path = tmp_path / "cross_platform.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    _setenv_common(monkeypatch)
    monkeypatch.delenv("DEBUG_MAX_ACCOUNTS", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "GHLClient", _no_ghl)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    monkeypatch.setattr(mod, "MetaAdsClient", _FakeMetaAdsClient)
    _FakeAtlasClient.accounts = [
        _atlas_account("Live Via Meta Co", stage="live", google_ads_customer_id="1234567890", meta_ad_account_id="act_555")
    ]
    _FakeGoogleAdsClient.responses = {"1234567890": _spend(total_cost=0.0, enabled_campaign_count=1)}
    _FakeMetaAdsClient.responses = {"act_555": _spend(total_cost=99.0, enabled_campaign_count=1)}
    _FakeSlack.sent = []

    init_db()
    db = get_session_factory()()
    try:
        run = mod.run_daily_go_live_audit(db)
        flags = db.query(mod.Flag).filter_by(run_id=run.id).all()

        assert [f for f in flags if f.category == FlagCategory.ads_off_should_be_on] == []
        # Still correctly flags Google's own zero-spend campaign, independently.
        zero_spend = [f for f in flags if f.category == FlagCategory.ads_off_zero_spend]
        assert len(zero_spend) == 1
        assert "Google Ads" in zero_spend[0].message
    finally:
        db.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
