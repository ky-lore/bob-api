import json
from datetime import datetime

from app.models import Flag, FlagCategory, FlagSeverity
from app.tasks.dashboard_summary import build_dashboard_json, waiting_to_go_live_candidates


def _flag(**kwargs) -> Flag:
    defaults = dict(
        run_id=1,
        category=FlagCategory.clock_violation,
        severity=FlagSeverity.warning,
        client_name="Acme Co",
        message="Marketing package: 14d, not live",
        created_at=datetime.utcnow(),
    )
    defaults.update(kwargs)
    return Flag(**defaults)


def test_waiting_to_go_live_only_includes_marketing_package_clock_violations(monkeypatch):
    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_blocking_narratives", lambda accounts: ({}, []))

    flags = [
        _flag(client_name="Acme Co", category=FlagCategory.clock_violation, message="Marketing package: 14d, not live"),
        _flag(client_name="Beta LLC", category=FlagCategory.clock_violation, message="Website package: 10d, not live"),
    ]
    account_context = {
        "Acme Co": {"day": 14, "stage": "onboarding", "package": "pkg-mktg"},
        "Beta LLC": {"day": 10, "stage": "development", "package": "pkg-web"},
    }
    result = json.loads(build_dashboard_json(flags, account_context, {"Acme Co", "Beta LLC"}, set(), set()))

    accounts = [r["account"] for r in result["waiting_to_go_live"]]
    assert accounts == ["Acme Co"]  # Beta LLC is pkg-web, not pkg-mktg -- excluded


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


def test_website_lane_stat_tile_counts_web_packages():
    account_context = {
        "A": {"day": 0, "stage": "x", "package": "pkg-web"},
        "B": {"day": 0, "stage": "x", "package": "pkg-web-custom"},
        "C": {"day": 0, "stage": "x", "package": "pkg-free-promo"},
        "D": {"day": 0, "stage": "x", "package": "pkg-mktg"},
    }
    result = json.loads(build_dashboard_json([], account_context, set(), set(), set()))
    assert result["stat_tiles"]["website_lane_builds"] == 3


def test_behind_14_day_target_only_counts_waiting_to_go_live_rows(monkeypatch):
    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_blocking_narratives", lambda accounts: ({}, []))

    flags = [_flag(client_name="Acme Co", message="Marketing package: 21d, not live")]
    account_context = {"Acme Co": {"day": 21, "stage": "onboarding", "package": "pkg-mktg"}}
    result = json.loads(build_dashboard_json(flags, account_context, {"Acme Co"}, set(), set()))

    assert result["stat_tiles"]["behind_14_day_target"] == 1


def test_accounts_chart_classifies_by_package_and_day():
    account_context = {
        "Acme Co": {"day": 21, "stage": "onboarding", "package": "pkg-mktg"},  # past target
        "Beta LLC": {"day": 5, "stage": "onboarding", "package": "pkg-mktg"},  # approaching
        "Gamma Inc": {"day": 30, "stage": "onboarding", "package": "pkg-web"},  # exempt, despite high day count
    }
    result = json.loads(build_dashboard_json([], account_context, set(), set(), set()))

    by_account = {r["account"]: r for r in result["accounts_chart"]}
    assert by_account["Acme Co"]["status"] == "critical"
    assert by_account["Beta LLC"]["status"] == "warning"
    assert by_account["Gamma Inc"]["status"] == "exempt"
    # oldest first
    assert [r["account"] for r in result["accounts_chart"]] == ["Gamma Inc", "Acme Co", "Beta LLC"]
    assert result["clock_threshold_days"] == 14


def test_waiting_to_go_live_candidates_matches_build_dashboard_json_selection():
    flags = [
        _flag(client_name="Acme Co", category=FlagCategory.clock_violation, message="Marketing package: 14d, not live"),
        _flag(client_name="Beta LLC", category=FlagCategory.clock_violation, message="Website package: 10d, not live"),
    ]
    account_context = {
        "Acme Co": {"day": 14, "stage": "onboarding", "package": "pkg-mktg"},
        "Beta LLC": {"day": 10, "stage": "development", "package": "pkg-web"},
    }
    assert waiting_to_go_live_candidates(flags, account_context) == ["Acme Co"]


def test_rich_context_is_appended_to_llm_context_for_waiting_candidates(monkeypatch):
    captured = {}

    def _fake_synthesize(accounts):
        captured["accounts"] = accounts
        return {}, []

    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_blocking_narratives", _fake_synthesize)

    flags = [_flag(client_name="Acme Co", message="Marketing package: 14d, not live")]
    account_context = {"Acme Co": {"day": 14, "stage": "onboarding", "package": "pkg-mktg"}}
    rich_context = {"Acme Co": ["[ClickUp comment, task card1] waiting on access", "[Slack #internal-acme-co] hi team"]}

    build_dashboard_json(flags, account_context, {"Acme Co"}, set(), set(), rich_context)

    acme_context = captured["accounts"][0]["context"]
    assert "Marketing package: 14d, not live" in acme_context
    assert "[ClickUp comment, task card1] waiting on access" in acme_context
    assert "[Slack #internal-acme-co] hi team" in acme_context


def test_rich_context_defaults_to_empty_without_breaking_existing_callers(monkeypatch):
    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_blocking_narratives", lambda accounts: ({}, []))
    flags = [_flag(client_name="Acme Co", message="Marketing package: 14d, not live")]
    account_context = {"Acme Co": {"day": 14, "stage": "onboarding", "package": "pkg-mktg"}}

    # No rich_context arg at all -- must not raise, must behave exactly as before.
    result = json.loads(build_dashboard_json(flags, account_context, {"Acme Co"}, set(), set()))
    assert result["waiting_to_go_live"][0]["account"] == "Acme Co"


def test_narrative_batches_are_surfaced_in_the_result(monkeypatch):
    fake_batches = [{"batch_index": 0, "accounts": ["Acme Co"], "narrated_count": 1, "ok": True, "error": None}]
    monkeypatch.setattr(
        "app.tasks.dashboard_summary.synthesize_blocking_narratives",
        lambda accounts: ({"Acme Co": "narrative"}, fake_batches),
    )

    flags = [_flag(client_name="Acme Co", message="Marketing package: 14d, not live")]
    account_context = {"Acme Co": {"day": 14, "stage": "onboarding", "package": "pkg-mktg"}}
    result = json.loads(build_dashboard_json(flags, account_context, {"Acme Co"}, set(), set()))

    assert result["narrative_batches"] == fake_batches


def test_build_dashboard_json_surfaces_llm_failure_instead_of_swallowing_it(monkeypatch):
    # Real bug: the first version of synthesize_blocking_narratives caught its
    # own exception and returned a silent fallback, so a real API failure was
    # completely invisible anywhere. narrative_error must be populated whenever
    # the LLM call fails, and rows must still fall back to raw flag messages.
    def _raise(accounts):
        raise RuntimeError("Claude response had no submit_narratives tool call (stop_reason=max_tokens)")

    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_blocking_narratives", _raise)

    flags = [_flag(client_name="Acme Co", message="Marketing package: 14d, not live")]
    account_context = {"Acme Co": {"day": 14, "stage": "onboarding", "package": "pkg-mktg"}}
    result = json.loads(build_dashboard_json(flags, account_context, {"Acme Co"}, set(), set()))

    assert result["narrative_error"] is not None
    assert "max_tokens" in result["narrative_error"]
    assert result["waiting_to_go_live"][0]["blocking"] == "Marketing package: 14d, not live"
