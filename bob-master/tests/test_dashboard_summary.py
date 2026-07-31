import json
from datetime import datetime

from app.models import Flag, FlagCategory, FlagSeverity
from app.tasks.dashboard_summary import build_dashboard_json


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


def test_build_dashboard_json_computes_stat_tiles_and_falls_back_without_llm(monkeypatch):
    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_blocking_narratives", lambda accounts: {})

    flags = [
        _flag(client_name="Acme Co", category=FlagCategory.clock_violation, message="Marketing package: 14d, not live"),
        _flag(client_name="Beta LLC", category=FlagCategory.heartbeat_mismatch, message="Google Ads: legacy campaign burning client budget"),
        _flag(client_name="Gamma Inc", category=FlagCategory.new_deal, message="Closed Won in GHL"),
    ]
    account_context = {"Acme Co": {"day": 14, "stage": "onboarding"}}
    all_account_names = {"Acme Co", "Beta LLC", "Gamma Inc", "Delta Co"}

    result = json.loads(build_dashboard_json(flags, account_context, all_account_names))

    assert result["stat_tiles"]["flagged_accounts"] == 3
    assert result["stat_tiles"]["clock_violations"] == 1
    assert result["stat_tiles"]["heartbeat_mismatches"] == 1
    assert result["stat_tiles"]["new_deals"] == 1
    assert result["stat_tiles"]["total_accounts_tracked"] == 4

    acme_row = next(r for r in result["rows"] if r["account"] == "Acme Co")
    assert acme_row["day"] == 14
    assert acme_row["stage"] == "onboarding"
    # Falls back to joining the raw flag messages when the LLM returns nothing for this account.
    assert "Marketing package: 14d, not live" in acme_row["blocking"]


def test_build_dashboard_json_uses_llm_narrative_when_available(monkeypatch):
    monkeypatch.setattr(
        "app.tasks.dashboard_summary.synthesize_blocking_narratives",
        lambda accounts: {"Acme Co": "Synthesized: stuck in onboarding, 14 days with no live spend."},
    )

    flags = [_flag(client_name="Acme Co")]
    result = json.loads(build_dashboard_json(flags, {"Acme Co": {"day": 14, "stage": "onboarding"}}, {"Acme Co"}))

    assert result["rows"][0]["blocking"] == "Synthesized: stuck in onboarding, 14 days with no live spend."


def test_build_dashboard_json_surfaces_llm_failure_instead_of_swallowing_it(monkeypatch):
    # Real bug: the first version of synthesize_blocking_narratives caught its
    # own exception and returned a silent fallback, so a real API failure was
    # completely invisible anywhere — not in notes, not in the dashboard, not
    # in logs. narrative_error must be populated whenever the LLM call fails,
    # and rows must still fall back to the raw flag messages, not go blank.
    def _raise(accounts):
        raise RuntimeError("Claude response had no submit_narratives tool call (stop_reason=max_tokens)")

    monkeypatch.setattr("app.tasks.dashboard_summary.synthesize_blocking_narratives", _raise)

    flags = [_flag(client_name="Acme Co", message="Marketing package: 14d, not live")]
    result = json.loads(build_dashboard_json(flags, {"Acme Co": {"day": 14, "stage": "onboarding"}}, {"Acme Co"}))

    assert result["narrative_error"] is not None
    assert "max_tokens" in result["narrative_error"]
    assert result["rows"][0]["blocking"] == "Marketing package: 14d, not live"


def test_build_dashboard_json_rows_sorted_oldest_first():
    flags = [
        _flag(client_name="Newer", message="msg"),
        _flag(client_name="Older", message="msg"),
    ]
    account_context = {"Newer": {"day": 3, "stage": "x"}, "Older": {"day": 40, "stage": "x"}}
    result = json.loads(build_dashboard_json(flags, account_context, {"Newer", "Older"}))

    assert [r["account"] for r in result["rows"]] == ["Older", "Newer"]
