"""
Tests app.tasks.atlas_report.build_atlas_report with every external client
faked -- proves the atlas_id passthrough, the compressed google_ads/meta_ads
shape (enabled campaigns only, not full REMOVED history), the deterministic
combined ad_spend, the health/status/recent_work split reaching each record,
the Zoom transcript wiring, and soft-fail behavior when a customer_id is bad.
"""
from datetime import datetime, timedelta, timezone

import pytest

import app.tasks.atlas_report as mod
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ZoomCallRecord


def _atlas_account(
    company_name,
    *,
    atlas_id=None,
    stage="live",
    google_ads_customer_id=None,
    meta_ad_account_id=None,
    clickup_folder_id=None,
    slack_channel_id=None,
):
    created_at = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "id": atlas_id or company_name.lower().replace(" ", "-"),
        "companyName": company_name,
        "stage": stage,
        "isActive": True,
        "createdAt": created_at,
        "integrations": {
            "clickupFolderId": clickup_folder_id,
            "slackChannelId": slack_channel_id,
            "googleMccId": google_ads_customer_id,
            "metaAdAccountId": meta_ad_account_id,
        },
    }


class _FakeAtlasClient:
    accounts: list = []

    def get_all_accounts(self):
        return _FakeAtlasClient.accounts


class _FakeClickUp:
    """Empty by default (existing tests don't care about ClickUp content);
    set the class-level fixtures below to exercise the folder/list/task/
    comment walk for the recent_clickup_activity tests."""

    lists: list = []
    tasks_by_list: dict = {}
    comments_by_task: dict = {}

    def get_folder_lists(self, folder_id):
        return _FakeClickUp.lists

    def get_list_tasks(self, list_id, include_closed=True, page=0):
        return {"tasks": _FakeClickUp.tasks_by_list.get(list_id, [])}

    def get_task_comments(self, task_id):
        return _FakeClickUp.comments_by_task.get(task_id, [])


class _FakeSlack:
    def channel_history(self, channel_id, oldest_ts=None):
        return []


class _FakeGoogleAdsClient:
    responses: dict = {}

    def get_account_spend(self, customer_id, date_range="YESTERDAY"):
        if customer_id not in _FakeGoogleAdsClient.responses:
            raise RuntimeError(f"no fake response for {customer_id}")
        return _FakeGoogleAdsClient.responses[customer_id]


class _FakeMetaAdsClient:
    responses: dict = {}

    def get_account_spend(self, ad_account_id, date_range="YESTERDAY"):
        if ad_account_id not in _FakeMetaAdsClient.responses:
            raise RuntimeError(f"no fake response for {ad_account_id}")
        return _FakeMetaAdsClient.responses[ad_account_id]


def _spend(total_cost, campaigns, *, id_key="customer_id", id_value="1234567890", total_conversions=1.0):
    return {
        id_key: id_value,
        "date_range": "LAST_7_DAYS",
        "total_cost": total_cost,
        "total_impressions": 100,
        "total_clicks": 5,
        "total_conversions": total_conversions,
        "total_conversions_value": 1.0,
        "enabled_campaign_count": sum(1 for c in campaigns if c["status"] == "ENABLED"),
        "campaigns": campaigns,
    }


_CAMPAIGN = {
    "id": "1", "name": "Active", "status": "ENABLED", "channel_type": "SEARCH", "cost": 50.0,
    "impressions": 100, "clicks": 5, "ctr": 0.05, "avg_cpc": 10.0, "conversions": 1.0,
    "cost_per_conversion": 50.0, "conversions_value": 1.0,
}
_DEAD_CAMPAIGN = {
    "id": "2", "name": "Dead", "status": "REMOVED", "channel_type": "SEARCH", "cost": 0.0,
    "impressions": 0, "clicks": 0, "ctr": 0.0, "avg_cpc": 0.0, "conversions": 0.0,
    "cost_per_conversion": 0.0, "conversions_value": 0.0,
}


def _setup(monkeypatch):
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(mod, "GoogleAdsClient", _FakeGoogleAdsClient)
    monkeypatch.setattr(mod, "MetaAdsClient", _FakeMetaAdsClient)
    _FakeAtlasClient.accounts = []
    _FakeGoogleAdsClient.responses = {}
    _FakeMetaAdsClient.responses = {}
    _FakeClickUp.lists = []
    _FakeClickUp.tasks_by_list = {}
    _FakeClickUp.comments_by_task = {}


@pytest.fixture
def db_session(tmp_path):
    """Real (temp file) SQLite session, same convention as the daily audit's
    e2e tests -- needed for the Zoom-context tests, which query ZoomCallRecord
    via real ORM chaining that a hand-rolled fake can't easily reproduce.

    Builds its own engine directly rather than going through
    app.db.get_engine/get_settings (both @lru_cache'd process-wide, per
    app/config.py and app/db.py) -- get_settings() in particular is never
    cache_clear()'d anywhere, so a DATABASE_URL env var set here would only
    ever take effect on whichever test in the whole run happens to call
    get_settings() first; every db_session test after that would silently
    share THAT test's tmp_path sqlite file instead of getting its own,
    accumulating rows across tests until two happened to collide on the
    same primary/unique key (confirmed the hard way, 2026-09-25, adding the
    standup-context tests below -- two tests each using a literal
    "standup-uuid-1" meeting_uuid passed in isolation but one failed with a
    UNIQUE constraint violation when run after the other)."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_record_carries_the_atlas_id_and_compressed_google_ads_summary(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Acme Co", atlas_id="acme-123", google_ads_customer_id="1234567890")]
    _FakeGoogleAdsClient.responses = {
        "1234567890": _spend(50.0, [_CAMPAIGN, _DEAD_CAMPAIGN], id_value="1234567890"),
    }
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts, on_batch_done=None: (
            {"Acme Co": {"health": "on_track", "status": "Live and spending", "recent_work": "Fixed the landing page"}},
            [],
        ),
    )

    records, batch_results = mod.build_atlas_report()

    assert len(records) == 1
    record = records[0]
    assert record["atlas_id"] == "acme-123"
    assert record["company_name"] == "Acme Co"
    assert record["is_live"] is True
    assert record["health"] == "on_track"
    assert record["status"] == "Live and spending"
    assert record["recent_work"] == "Fixed the landing page"
    # Compressed: only the enabled campaign survives, not the removed one.
    assert len(record["google_ads"]["enabled_campaigns"]) == 1
    assert record["google_ads"]["enabled_campaigns"][0]["name"] == "Active"
    assert record["google_ads"]["total_cost"] == 50.0
    # No Meta ID on file -> null, and combined spend falls back to Google alone.
    assert record["meta_ads"] is None
    assert record["ad_spend"] == {"total_spend": 50.0, "total_conversions": 1.0, "cost_per_conversion": 50.0}


def test_account_with_no_google_mcc_id_gets_a_null_google_ads_field_not_an_error(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("No CID Co", google_ads_customer_id=None)]
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts, on_batch_done=None: ({}, []))

    records, _ = mod.build_atlas_report()

    assert records[0]["google_ads"] is None
    assert records[0]["google_ads_error"] is None
    assert records[0]["is_live"] is True  # falls back to Atlas's own stage == "live"
    assert records[0]["health"] == "on_track"  # default when synthesis produced nothing for this account
    assert records[0]["ad_spend"] is None  # neither platform on file


def test_is_live_reflects_atlas_stage_not_ad_spend(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Not Live Co", stage="onboarding", google_ads_customer_id="123")]
    _FakeGoogleAdsClient.responses = {"123": _spend(0.0, [], id_value="123", total_conversions=0.0)}
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts, on_batch_done=None: ({}, []))

    records, _ = mod.build_atlas_report()

    assert records[0]["is_live"] is False


def test_bad_customer_id_is_soft_failed_not_run_crashing(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Bad CID Co", google_ads_customer_id="9999999999")]
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts, on_batch_done=None: ({}, []))

    records, _ = mod.build_atlas_report()

    assert records[0]["google_ads"] is None
    assert "no fake response" in records[0]["google_ads_error"]


def test_limit_caps_the_universe_sorted_by_company_name(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Charlie"), _atlas_account("Alpha"), _atlas_account("Beta")]
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts, on_batch_done=None: ({}, []))

    records, _ = mod.build_atlas_report(limit=2)

    assert [r["company_name"] for r in records] == ["Alpha", "Beta"]


def test_meta_ads_summary_is_compressed_and_combined_with_google_into_ad_spend(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [
        _atlas_account("Both Platforms Co", google_ads_customer_id="111", meta_ad_account_id="act_222"),
    ]
    _FakeGoogleAdsClient.responses = {"111": _spend(50.0, [_CAMPAIGN], id_value="111", total_conversions=2.0)}
    _FakeMetaAdsClient.responses = {
        "act_222": _spend(30.0, [_CAMPAIGN, _DEAD_CAMPAIGN], id_key="ad_account_id", id_value="act_222", total_conversions=1.0),
    }
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts, on_batch_done=None: ({}, []))

    records, _ = mod.build_atlas_report()

    record = records[0]
    assert record["meta_ads"]["ad_account_id"] == "act_222"
    assert len(record["meta_ads"]["enabled_campaigns"]) == 1
    assert record["ad_spend"] == {"total_spend": 80.0, "total_conversions": 3.0, "cost_per_conversion": pytest.approx(80.0 / 3.0)}


def test_bad_meta_ad_account_id_is_soft_failed_not_run_crashing(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Bad Meta Co", meta_ad_account_id="act_999")]
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts, on_batch_done=None: ({}, []))

    records, _ = mod.build_atlas_report()

    assert records[0]["meta_ads"] is None
    assert "no fake response" in records[0]["meta_ads_error"]


def test_health_field_passes_through_from_synthesis(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("At Risk Co", atlas_id="risk-1")]
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts, on_batch_done=None: ({"At Risk Co": {"health": "at_risk", "status": "Escalated", "recent_work": "x"}}, []),
    )

    records, _ = mod.build_atlas_report()

    assert records[0]["health"] == "at_risk"


def test_no_db_session_skips_zoom_but_does_not_error(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("No DB Co", atlas_id="no-db-1")]
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts, on_batch_done=None: ({}, []))

    records, _ = mod.build_atlas_report(db=None)

    assert records[0]["zoom_call_count"] == 0


def test_zoom_transcript_within_the_window_reaches_the_narrative_context_and_count(monkeypatch, db_session):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Zoom Co", atlas_id="zoom-1")]
    db_session.add(ZoomCallRecord(
        meeting_uuid="uuid-recent",
        host_email="am@advancedmarketers.co",
        topic="Zoom Co Weekly Check-in",
        start_time=datetime.now(timezone.utc) - timedelta(days=2),
        atlas_account_id="zoom-1",
        matched_company_name="Zoom Co",
        match_confidence=0.95,
        transcript_text="WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\nAM: Let's review the campaign budget.",
        pulled_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    captured = []
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts, on_batch_done=None: (captured.extend(accounts) or {}, []),
    )

    records, _ = mod.build_atlas_report(db=db_session)

    assert records[0]["zoom_call_count"] == 1
    zoom_context = [c for c in captured[0]["context"] if c.startswith("[Zoom call")]
    assert len(zoom_context) == 1
    assert "review the campaign budget" in zoom_context[0]


def test_zoom_transcript_outside_the_window_is_excluded(monkeypatch, db_session):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Stale Zoom Co", atlas_id="stale-1")]
    db_session.add(ZoomCallRecord(
        meeting_uuid="uuid-stale",
        host_email="am@advancedmarketers.co",
        topic="Stale Zoom Co Kickoff",
        start_time=datetime.now(timezone.utc) - timedelta(days=45),
        atlas_account_id="stale-1",
        matched_company_name="Stale Zoom Co",
        match_confidence=0.95,
        transcript_text="WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\nAM: Kickoff notes from ages ago.",
        pulled_at=datetime.now(timezone.utc),
    ))
    db_session.commit()
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts, on_batch_done=None: ({}, []))

    records, _ = mod.build_atlas_report(db=db_session)

    assert records[0]["zoom_call_count"] == 0


def _standup_call(meeting_uuid, transcript_text, days_ago=1):
    return ZoomCallRecord(
        meeting_uuid=meeting_uuid,
        host_email="chris@advancedmarketers.co",
        topic=mod._STANDUP_TOPIC,
        start_time=datetime.now(timezone.utc) - timedelta(days=days_ago),
        atlas_account_id=None,
        matched_company_name=None,
        match_confidence=None,
        transcript_text=transcript_text,
        pulled_at=datetime.now(timezone.utc),
    )


def test_standup_mention_reaches_the_narrative_context_but_not_the_call_count(monkeypatch, db_session):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Vizeon Construction", atlas_id="vizeon-1")]
    db_session.add(_standup_call(
        "standup-uuid-1",
        "WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\n"
        "Chris: Vizeon asked to pause the campaign for a few weeks.",
    ))
    db_session.commit()

    captured = []
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts, on_batch_done=None: (captured.extend(accounts) or {}, []),
    )

    records, _ = mod.build_atlas_report(db=db_session)

    # Not a client-hosted call -- must not inflate the "N call transcripts
    # this wk" chip, even though it did add real content to the LLM context.
    assert records[0]["zoom_call_count"] == 0
    standup_context = [c for c in captured[0]["context"] if mod._STANDUP_TOPIC in c]
    assert len(standup_context) == 1
    assert "pause the campaign" in standup_context[0]


def test_standup_mention_outside_the_window_is_excluded(monkeypatch, db_session):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Vizeon Construction", atlas_id="vizeon-1")]
    db_session.add(_standup_call(
        "standup-uuid-stale",
        "WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\nChris: Vizeon update from ages ago.",
        days_ago=45,
    ))
    db_session.commit()
    captured = []
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts, on_batch_done=None: (captured.extend(accounts) or {}, []),
    )

    records, _ = mod.build_atlas_report(db=db_session)

    standup_context = [c for c in captured[0]["context"] if mod._STANDUP_TOPIC in c]
    assert standup_context == []


def test_standup_mentions_are_capped_per_account(monkeypatch, db_session):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Vizeon Construction", atlas_id="vizeon-1")]
    for i in range(4):
        db_session.add(_standup_call(
            f"standup-uuid-{i}",
            f"WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\nChris: Vizeon update number {i}.",
            days_ago=i + 1,
        ))
    db_session.commit()
    captured = []
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts, on_batch_done=None: (captured.extend(accounts) or {}, []),
    )

    mod.build_atlas_report(db=db_session)

    standup_context = [c for c in captured[0]["context"] if mod._STANDUP_TOPIC in c]
    assert len(standup_context) == mod._STANDUP_MENTION_LIMIT


def test_short_first_word_company_name_skips_standup_matching_entirely(monkeypatch, db_session):
    """Guards against noisy false-positive matches for short/generic first
    words (e.g. "LG", "OC", "LA") -- see _standup_search_key."""
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("LG Electric", atlas_id="lg-1")]
    db_session.add(_standup_call(
        "standup-uuid-lg",
        "WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\nChris: lg is a common substring in lots of words.",
    ))
    db_session.commit()
    captured = []
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts, on_batch_done=None: (captured.extend(accounts) or {}, []),
    )

    mod.build_atlas_report(db=db_session)

    standup_context = [c for c in captured[0]["context"] if mod._STANDUP_TOPIC in c]
    assert standup_context == []


def test_run_and_store_atlas_report_persists_a_queryable_row(monkeypatch, db_session):
    import json

    from app.models import AtlasReportRun

    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Persisted Co", atlas_id="persisted-1")]
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts, on_batch_done=None: ({"Persisted Co": {"health": "needs_attention", "status": "x", "recent_work": "y"}}, []),
    )

    run = mod.run_and_store_atlas_report(db_session, limit=5)

    assert run.id is not None
    assert run.limit_used == 5
    data = json.loads(run.report_json)
    assert data["count"] == 1
    assert data["accounts"][0]["company_name"] == "Persisted Co"
    assert data["accounts"][0]["health"] == "needs_attention"

    # Actually queryable back out, not just returned in-memory.
    stored = db_session.query(AtlasReportRun).filter_by(id=run.id).one()
    assert json.loads(stored.report_json)["accounts"][0]["company_name"] == "Persisted Co"


def test_on_progress_reports_each_account_gathered_then_each_synthesis_batch(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Alpha Co"), _atlas_account("Beta Co")]

    def _fake_reports(accounts, on_batch_done=None):
        if on_batch_done:
            on_batch_done(1, 1)
        return {}, []

    monkeypatch.setattr(mod, "synthesize_account_reports", _fake_reports)

    events = []
    mod.build_atlas_report(on_progress=events.append)

    gathering = [e for e in events if e["phase"] == "gathering"]
    assert [e["completed"] for e in gathering] == [1, 2]
    assert gathering[0]["total"] == 2
    assert gathering[1]["account"] == "Beta Co"

    synthesizing = [e for e in events if e["phase"] == "synthesizing"]
    assert synthesizing[-1] == {"phase": "synthesizing", "completed": 1, "total": 1}


def test_on_progress_errors_never_break_the_run(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Alpha Co")]
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts, on_batch_done=None: ({}, []))

    def _broken_progress(payload):
        raise RuntimeError("progress sink is down")

    records, _ = mod.build_atlas_report(on_progress=_broken_progress)

    assert len(records) == 1


def test_health_override_wins_over_llm_health(monkeypatch, db_session):
    from app.models import AccountHealthOverride

    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Overridden Co", atlas_id="ov-1")]
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts, on_batch_done=None: ({"Overridden Co": {"health": "on_track", "status": "x", "recent_work": "y"}}, []),
    )
    db_session.add(AccountHealthOverride(
        atlas_id="ov-1", company_name="Overridden Co", health="at_risk", reason="Known churn risk",
        set_by="bob", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    records, _ = mod.build_atlas_report(db=db_session)

    assert records[0]["health"] == "at_risk"
    assert records[0]["llm_health"] == "on_track"
    assert records[0]["health_overridden"] is True
    assert records[0]["health_override_reason"] == "Known churn risk"


def test_no_override_leaves_llm_health_untouched(monkeypatch, db_session):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Plain Co", atlas_id="plain-1")]
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts, on_batch_done=None: ({"Plain Co": {"health": "needs_attention", "status": "x", "recent_work": "y"}}, []),
    )

    records, _ = mod.build_atlas_report(db=db_session)

    assert records[0]["health"] == "needs_attention"
    assert records[0]["health_overridden"] is False
    assert records[0]["health_override_reason"] is None
    assert "llm_health" not in records[0]


def test_no_db_session_skips_overrides_gracefully(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("No DB Override Co", atlas_id="no-db-ov")]
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts, on_batch_done=None: ({}, []))

    records, _ = mod.build_atlas_report(db=None)

    assert records[0]["health_overridden"] is False


def test_recent_clickup_activity_threads_through_to_the_record(monkeypatch):
    """Real ask, 2026-09-21: "tasks active in the last 48hrs of weekdays...
    referenceable in the card." Proves build_atlas_report's record carries
    the structured recent_clickup_activity list gather_atlas_context
    produces -- the actual weekend-aware cutoff logic itself is unit-tested
    directly in test_account_context_gather.py."""
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Task Co", atlas_id="task-co-1", clickup_folder_id="folder1")]
    _FakeClickUp.lists = [{"id": "list1"}]
    _FakeClickUp.tasks_by_list = {"list1": [{"id": "task1", "name": "Fix redirect", "date_updated": now_ms}]}
    _FakeClickUp.comments_by_task = {"task1": [{"comment_text": "deployed the fix", "date": now_ms}]}
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts, on_batch_done=None: ({}, []))

    records, _ = mod.build_atlas_report()

    activity = records[0]["recent_clickup_activity"]
    assert len(activity) == 1
    assert activity[0]["task_id"] == "task1"
    assert activity[0]["task_name"] == "Fix redirect"
    assert activity[0]["text"] == "deployed the fix"


def test_no_clickup_folder_id_leaves_recent_clickup_activity_empty(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("No Folder Co", atlas_id="no-folder-1", clickup_folder_id=None)]
    monkeypatch.setattr(mod, "synthesize_account_reports", lambda accounts, on_batch_done=None: ({}, []))

    records, _ = mod.build_atlas_report()

    assert records[0]["recent_clickup_activity"] == []


def test_evidence_quotes_thread_through_to_the_record(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Quote Co", atlas_id="quote-co-1")]
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts, on_batch_done=None: (
            {
                "Quote Co": {
                    "health": "on_track",
                    "status": "Live and spending",
                    "recent_work": "Fixed the landing page",
                    "evidence": [{"source": "slack", "quote": "landing page is fixed now"}],
                }
            },
            [],
        ),
    )

    records, _ = mod.build_atlas_report()

    assert records[0]["evidence"] == [{"source": "slack", "quote": "landing page is fixed now"}]


def test_missing_evidence_key_defaults_to_an_empty_list_on_the_record(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("No Evidence Co", atlas_id="no-evidence-1")]
    monkeypatch.setattr(
        mod, "synthesize_account_reports",
        lambda accounts, on_batch_done=None: (
            {"No Evidence Co": {"health": "on_track", "status": "x", "recent_work": "y"}},
            [],
        ),
    )

    records, _ = mod.build_atlas_report()

    assert records[0]["evidence"] == []
