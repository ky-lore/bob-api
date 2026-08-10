import json
from datetime import datetime

from app.models import Flag, FlagCategory, FlagSeverity
from app.tasks.dashboard_summary import all_matched_accounts, build_dashboard_json


def _flag(**kwargs) -> Flag:
    defaults = dict(
        run_id=1,
        category=FlagCategory.ads_off_zero_spend,
        severity=FlagSeverity.warning,
        client_name="Acme Co",
        message="Meta: campaigns enabled, $0 spend — billing/payment check",
        created_at=datetime.utcnow(),
    )
    defaults.update(kwargs)
    return Flag(**defaults)


def test_accounts_overview_includes_every_matched_account_regardless_of_live_status(monkeypatch):
    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_account_narratives", lambda accounts: ({}, []))

    account_context = {
        "Acme Co": {"day": 21, "stage": "onboarding", "card_id": "card1"},
        "Beta LLC": {"day": 10, "stage": "development", "card_id": "card2"},
    }
    result = json.loads(build_dashboard_json([], account_context, {"Acme Co", "Beta LLC"}, {"Beta LLC"}, set()))

    accounts = {r["account"] for r in result["accounts_overview"]}
    assert accounts == {"Acme Co", "Beta LLC"}  # both included, live or not -- no package filtering anymore
    by_account = {r["account"]: r for r in result["accounts_overview"]}
    assert by_account["Acme Co"]["is_live"] is False
    assert by_account["Beta LLC"]["is_live"] is True


def test_all_matched_accounts_returns_every_account_context_key():
    account_context = {
        "Acme Co": {"day": 14, "stage": "onboarding", "card_id": "card1"},
        "Beta LLC": {"day": 10, "stage": "development", "card_id": "card2"},
    }
    assert sorted(all_matched_accounts(account_context)) == ["Acme Co", "Beta LLC"]


def test_ads_off_buckets_populated_from_flag_categories():
    flags = [
        _flag(client_name="Roof City Professionals", category=FlagCategory.ads_off_should_be_on,
              message="Board says live/optimizations but both platforms are dark — verify account mapping or billing"),
        _flag(client_name="5blox", category=FlagCategory.ads_off_zero_spend, message="Meta: campaigns enabled, $0 spend — billing/payment check"),
        _flag(client_name="Reel Electric", category=FlagCategory.ads_off_unsettled, message="Meta account status is UNSETTLED — payment is blocking ad delivery"),
        _flag(client_name="Ram Dumpster", category=FlagCategory.ads_off_verified_off, message="Retention shows churned/cancelled and heartbeat confirms no active spend — verified off"),
    ]
    result = json.loads(build_dashboard_json(flags, {}, set(), set(), set()))

    assert result["ads_off"]["should_be_on_but_dark"] == [
        {"account": "Roof City Professionals", "detail": flags[0].message}
    ]
    assert result["ads_off"]["campaigns_on_zero_spend"] == [
        {"account": "5blox", "platform": "Meta", "note": "campaigns enabled, $0 spend — billing/payment check"}
    ]
    assert result["ads_off"]["unsettled"][0]["account"] == "Reel Electric"
    assert result["ads_off"]["verified_off"][0]["account"] == "Ram Dumpster"


def test_campaigns_on_zero_spend_combines_multiple_platforms_for_same_account():
    # Next Era HVAC: "Google paused-verification; Meta 1 campaign $0" in the
    # reference dashboard is ONE combined row, not two separate ones.
    flags = [
        _flag(client_name="Next Era HVAC", category=FlagCategory.ads_off_zero_spend, message="Google Ads: campaigns enabled, $0 spend — billing/payment check"),
        _flag(client_name="Next Era HVAC", category=FlagCategory.ads_off_zero_spend, message="Meta: campaigns enabled, $0 spend — billing/payment check"),
    ]
    result = json.loads(build_dashboard_json(flags, {}, set(), set(), set()))

    assert len(result["ads_off"]["campaigns_on_zero_spend"]) == 1
    assert result["ads_off"]["campaigns_on_zero_spend"][0]["platform"] == "Google Ads + Meta"


def test_went_live_is_new_minus_previous_live_accounts():
    result = json.loads(
        build_dashboard_json([], {}, set(), {"Acme Co", "Beta LLC"}, {"Beta LLC"})
    )
    assert [r["account"] for r in result["went_live"]] == ["Acme Co"]


def test_live_accounts_stored_for_next_run_diff():
    result = json.loads(build_dashboard_json([], {}, set(), {"Acme Co", "Beta LLC"}, set()))
    assert sorted(result["live_accounts"]) == ["Acme Co", "Beta LLC"]


def test_stat_tiles_count_live_vs_not_live_with_no_package_distinction():
    account_context = {
        "A": {"day": 0, "stage": "x", "card_id": "1"},
        "B": {"day": 0, "stage": "x", "card_id": "2"},
        "C": {"day": 0, "stage": "x", "card_id": "3"},
    }
    result = json.loads(build_dashboard_json([], account_context, set(), {"A", "B"}, set()))
    assert result["stat_tiles"]["accounts_tracked"] == 3
    assert result["stat_tiles"]["live"] == 2
    assert result["stat_tiles"]["not_live"] == 1


def test_accounts_chart_is_live_vs_not_live_only_no_package_status():
    account_context = {
        "Acme Co": {"day": 21, "stage": "onboarding", "card_id": "1"},
        "Beta LLC": {"day": 5, "stage": "onboarding", "card_id": "2"},
        "Gamma Inc": {"day": 30, "stage": "onboarding", "card_id": "3"},
    }
    result = json.loads(build_dashboard_json([], account_context, set(), {"Beta LLC"}, set()))

    by_account = {r["account"]: r for r in result["accounts_chart"]}
    assert by_account["Beta LLC"]["status"] == "live"
    assert by_account["Acme Co"]["status"] == "not_live"
    assert by_account["Gamma Inc"]["status"] == "not_live"
    # oldest first
    assert [r["account"] for r in result["accounts_chart"]] == ["Gamma Inc", "Acme Co", "Beta LLC"]
    assert "clock_threshold_days" not in result


def test_rich_context_is_appended_to_llm_context_for_every_account(monkeypatch):
    captured = {}

    def _fake_synthesize(accounts):
        captured["accounts"] = accounts
        return {}, []

    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_account_narratives", _fake_synthesize)

    account_context = {"Acme Co": {"day": 14, "stage": "onboarding", "card_id": "card1"}}
    rich_context = {"Acme Co": ["[ClickUp comment, task card1] waiting on access", "[Slack #internal-acme-co] hi team"]}

    build_dashboard_json([], account_context, {"Acme Co"}, set(), set(), rich_context)

    acme_context = captured["accounts"][0]["context"]
    assert "[ClickUp comment, task card1] waiting on access" in acme_context
    assert "[Slack #internal-acme-co] hi team" in acme_context


def test_spend_by_account_flows_into_the_overview_and_llm_input(monkeypatch):
    captured = {}

    def _fake_synthesize(accounts):
        captured["accounts"] = accounts
        return {}, []

    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_account_narratives", _fake_synthesize)

    account_context = {"Acme Co": {"day": 14, "stage": "onboarding", "card_id": "card1"}}
    spend_by_account = {"Acme Co": {"Google Ads": {"spend": 123.45, "enabled_campaigns": 2}}}

    result = json.loads(
        build_dashboard_json([], account_context, {"Acme Co"}, set(), set(), None, spend_by_account)
    )

    assert result["accounts_overview"][0]["ad_spend"] == {"Google Ads": {"spend": 123.45, "enabled_campaigns": 2}}
    assert captured["accounts"][0]["ad_spend"] == {"Google Ads": {"spend": 123.45, "enabled_campaigns": 2}}


def test_rich_context_defaults_to_empty_without_breaking_existing_callers(monkeypatch):
    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_account_narratives", lambda accounts: ({}, []))
    account_context = {"Acme Co": {"day": 14, "stage": "onboarding", "card_id": "card1"}}

    # No rich_context/spend_by_account/web_builds args at all -- must not raise.
    result = json.loads(build_dashboard_json([], account_context, {"Acme Co"}, set(), set()))
    assert result["accounts_overview"][0]["account"] == "Acme Co"
    assert result["accounts_overview"][0]["ad_spend"] == {}
    assert result["web_builds"] == []


def test_web_builds_are_sorted_oldest_first_no_threshold_filtering(monkeypatch):
    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_account_narratives", lambda accounts: ({}, []))
    web_builds = [
        {"card_id": "b1", "name": "Fresh Site Co", "status": "development", "day": 3},
        {"card_id": "b2", "name": "ColdRiite Walk-Ins", "status": "development", "day": 374},
        {"card_id": "b3", "name": "Big Reds Tile", "status": "ready to launch", "day": 90},
    ]
    result = json.loads(build_dashboard_json([], {}, set(), set(), set(), None, None, web_builds))

    # oldest first, and nothing dropped -- no >30-day filter, this is macro/visibility only
    assert [b["name"] for b in result["web_builds"]] == ["ColdRiite Walk-Ins", "Big Reds Tile", "Fresh Site Co"]
    assert len(result["web_builds"]) == 3


def test_narrative_batches_are_surfaced_in_the_result(monkeypatch):
    fake_batches = [{"batch_index": 0, "accounts": ["Acme Co"], "narrated_count": 1, "ok": True, "error": None}]
    monkeypatch.setattr(
        "app.tasks.dashboard_summary.synthesize_account_narratives",
        lambda accounts: ({"Acme Co": "narrative"}, fake_batches),
    )

    account_context = {"Acme Co": {"day": 14, "stage": "onboarding", "card_id": "card1"}}
    result = json.loads(build_dashboard_json([], account_context, {"Acme Co"}, set(), set()))

    assert result["narrative_batches"] == fake_batches


def test_account_skipped_by_a_successful_batch_gets_clean_placeholder_not_raw_dump(monkeypatch):
    # Real production bug (2026-07-31): the batch call succeeds overall (no
    # exception, narrative_error stays None -- no warning banner) but the
    # model just doesn't produce an entry for one account, likely because its
    # busy Slack channel bloated that batch's input. The account still needs
    # SOMETHING in "Where they stand" -- it must never be the raw joined
    # ClickUp/Slack context (real example: 100+ lines including Slack
    # "has joined the channel" system messages pasted verbatim).
    monkeypatch.setattr(
        "app.tasks.dashboard_summary.synthesize_account_narratives",
        lambda accounts: ({}, [{"batch_index": 0, "accounts": [a["account"] for a in accounts], "narrated_count": 0, "ok": True, "error": None}]),
    )

    account_context = {"Roof City Professionals": {"day": 5, "stage": "onboarding", "card_id": "card1"}}
    rich_context = {
        "Roof City Professionals": [
            "[Slack #internal-roof_city_professionals] <@U0BLXKX8LS1> has joined the channel",
            "[Slack #internal-roof_city_professionals] hey team quick update",
        ]
    }
    result = json.loads(
        build_dashboard_json([], account_context, {"Roof City Professionals"}, set(), set(), rich_context)
    )

    status = result["accounts_overview"][0]["status"]
    assert "[Slack #" not in status
    assert "has joined the channel" not in status
    assert result["narrative_error"] is None  # this is the "no banner shown" part of the bug
    assert status == "Day 5, not live yet. Narrative unavailable this run (2 context item(s) gathered, not synthesized)."


def test_build_dashboard_json_surfaces_llm_failure_instead_of_swallowing_it(monkeypatch):
    # Real bug: the first version of the narrative synthesizer caught its own
    # exception and returned a silent fallback, so a real API failure was
    # completely invisible anywhere. narrative_error must be populated whenever
    # the LLM call fails, and rows must still get a clean placeholder -- never
    # the raw ClickUp/Slack context (a later, separate real bug: an account
    # with a busy Slack channel got 100+ raw lines pasted into its status).
    def _raise(accounts):
        raise RuntimeError("Claude response had no submit_narratives tool call (stop_reason=max_tokens)")

    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_account_narratives", _raise)

    flags = [_flag(client_name="Acme Co", category=FlagCategory.ads_off_zero_spend, message="Meta: campaigns enabled, $0 spend")]
    account_context = {"Acme Co": {"day": 14, "stage": "onboarding", "card_id": "card1"}}
    result = json.loads(build_dashboard_json(flags, account_context, {"Acme Co"}, set(), set()))

    assert result["narrative_error"] is not None
    assert "max_tokens" in result["narrative_error"]
    status = result["accounts_overview"][0]["status"]
    assert "Meta: campaigns enabled, $0 spend" not in status  # never the raw context, even in the message
    assert status == "Day 14, not live yet. Narrative unavailable this run (1 context item(s) gathered, not synthesized)."


def test_target_status_flows_into_accounts_chart_and_overview(monkeypatch):
    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_account_narratives", lambda accounts: ({}, []))
    account_context = {
        "Behind Co": {"day": 20, "stage": "onboarding", "target_status": "behind"},
        "Fresh Co": {"day": 2, "stage": "onboarding", "target_status": "on_track"},
    }
    result = json.loads(build_dashboard_json([], account_context, {"Behind Co", "Fresh Co"}, set(), set()))

    chart_by_account = {r["account"]: r for r in result["accounts_chart"]}
    assert chart_by_account["Behind Co"]["target_status"] == "behind"
    assert chart_by_account["Fresh Co"]["target_status"] == "on_track"

    overview_by_account = {r["account"]: r for r in result["accounts_overview"]}
    assert overview_by_account["Behind Co"]["target_status"] == "behind"


def test_target_status_defaults_to_unknown_when_absent():
    account_context = {"Acme Co": {"day": 5, "stage": "onboarding"}}
    result = json.loads(build_dashboard_json([], account_context, {"Acme Co"}, set(), set()))
    assert result["accounts_chart"][0]["target_status"] == "unknown"


def test_behind_stat_tile_counts_only_behind_target_status(monkeypatch):
    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_account_narratives", lambda accounts: ({}, []))
    account_context = {
        "Behind Co": {"day": 20, "stage": "onboarding", "target_status": "behind"},
        "Also Behind Co": {"day": 25, "stage": "onboarding", "target_status": "behind"},
        "Fresh Co": {"day": 2, "stage": "onboarding", "target_status": "on_track"},
        "Live Co": {"day": 10, "stage": "live", "target_status": "live"},
    }
    result = json.loads(build_dashboard_json([], account_context, set(account_context), set(), set()))
    assert result["stat_tiles"]["behind"] == 2


def test_ad_platform_errors_flows_into_accounts_overview(monkeypatch):
    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_account_narratives", lambda accounts: ({}, []))
    account_context = {"Acme Co": {"day": 14, "stage": "onboarding"}}
    ad_platform_errors = {"Acme Co": {"Google Ads": "Client error 401 Unauthorized"}}

    result = json.loads(
        build_dashboard_json([], account_context, {"Acme Co"}, set(), set(), None, None, None, ad_platform_errors)
    )

    assert result["accounts_overview"][0]["ad_spend_errors"] == {"Google Ads": "Client error 401 Unauthorized"}


def test_ad_platform_errors_defaults_to_empty_dict_without_breaking_existing_callers(monkeypatch):
    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_account_narratives", lambda accounts: ({}, []))
    account_context = {"Acme Co": {"day": 14, "stage": "onboarding"}}

    # No ad_platform_errors arg at all -- must not raise, must default to {}.
    result = json.loads(build_dashboard_json([], account_context, {"Acme Co"}, set(), set()))
    assert result["accounts_overview"][0]["ad_spend_errors"] == {}
