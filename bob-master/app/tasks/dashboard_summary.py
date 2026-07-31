"""
Builds the JSON stored on AuditRun.dashboard_json, structured to match the
reference dashboard (golive-pipeline-dashboard.pdf, provided 2026-07-31):
  - stat_tiles: the same 6 categories, computed deterministically
  - waiting_to_go_live: marketing-package accounts not yet live, oldest first,
    with an LLM-synthesized "what's blocking" sentence (see
    integrations/anthropic_client.py) — the only section that gets narrative
    prose, matching the original (the "ads off" tables are plain factual
    detail there, not synthesized sentences)
  - ads_off: the 4 sub-buckets from ads_off_classification.py
  - new_deals / went_live: plain factual lists
  - live_accounts: stored so the NEXT run can diff against it for went_live

The existing raw per-category flag list (rendered separately in dashboard.html
as a "detail" section) is untouched by this — this module only builds the
polished, reference-dashboard-shaped view.
"""
from __future__ import annotations

import json
from collections import defaultdict

from app.integrations.anthropic_client import synthesize_blocking_narratives
from app.models import Flag, FlagCategory

_WEBSITE_LANE_PACKAGES = {"pkg-web", "pkg-web-custom", "pkg-free-promo"}
_MKTG_BEHIND_DAYS = 14


def _group_flags_by_account(flags: list[Flag]) -> dict[str, list[Flag]]:
    by_account: dict[str, list[Flag]] = defaultdict(list)
    for f in flags:
        by_account[f.client_name].append(f)
    return by_account


def waiting_to_go_live_candidates(flags: list[Flag], account_context: dict[str, dict]) -> list[str]:
    """Marketing-package accounts with a clock violation (i.e. not live) —
    the only accounts that get an LLM-narrated "what's blocking" sentence.
    Exposed standalone (not just inlined in build_dashboard_json) so the
    caller can pre-fetch rich per-account context (ClickUp comments, Slack
    channel history — see account_context_gather.py) for exactly this set
    before build_dashboard_json runs, rather than for every account on the
    board."""
    by_account = _group_flags_by_account(flags)
    return [
        account_name
        for account_name, account_flags in by_account.items()
        if account_context.get(account_name, {}).get("package") == "pkg-mktg"
        and any(f.category == FlagCategory.clock_violation for f in account_flags)
    ]


def build_dashboard_json(
    flags: list[Flag],
    account_context: dict[str, dict],
    all_account_names: set[str],
    live_accounts: set[str],
    previous_live_accounts: set[str],
    rich_context: dict[str, list[str]] | None = None,
) -> str:
    """rich_context: optional {account_name: [context strings, ...]} — extra
    ClickUp/Slack material (see account_context_gather.py) appended after the
    deterministic flag messages for that account's LLM narrative input. Pure
    merge only; this function does no I/O of its own, per the module docstring."""
    by_account = _group_flags_by_account(flags)
    rich_context = rich_context or {}

    # --- Waiting to go live: marketing-package accounts with a clock
    # violation (i.e. not live), narrated via one batched LLM call ---
    waiting_candidates = waiting_to_go_live_candidates(flags, account_context)
    accounts_for_llm = [
        {
            "account": account_name,
            "day": account_context.get(account_name, {}).get("day", 0),
            "stage": account_context.get(account_name, {}).get("stage", "unknown"),
            "context": [f.message for f in by_account[account_name]] + rich_context.get(account_name, []),
        }
        for account_name in waiting_candidates
    ]

    narrative_error: str | None = None
    narrative_batches: list[dict] = []
    try:
        narratives, narrative_batches = synthesize_blocking_narratives(accounts_for_llm)
    except Exception as exc:
        # Degrade to the raw flag-message join per account (never blank the
        # dashboard over this), but surface *why* — silently swallowing this
        # is exactly what made the first real failure undiagnosable.
        narratives = {}
        narrative_error = f"{type(exc).__name__}: {exc}"

    waiting_to_go_live = [
        {
            "account": a["account"],
            "day": a["day"],
            "stage": a["stage"],
            "blocking": narratives.get(a["account"]) or "; ".join(a["context"]),
        }
        for a in accounts_for_llm
    ]
    waiting_to_go_live.sort(key=lambda r: r["day"], reverse=True)  # oldest first

    # --- Accounts chart: every board-matched account's day count, colored by
    # clock status (website-lane/free-promo packages are exempt from the
    # 14-day marketing clock entirely, not just "not yet behind") ---
    accounts_chart = [
        {
            "account": account_name,
            "day": ctx.get("day", 0),
            "status": (
                "exempt"
                if ctx.get("package") in _WEBSITE_LANE_PACKAGES
                else ("critical" if ctx.get("day", 0) >= _MKTG_BEHIND_DAYS else "warning")
            ),
        }
        for account_name, ctx in account_context.items()
    ]
    accounts_chart.sort(key=lambda r: r["day"], reverse=True)  # oldest first

    # --- Ads off: 4 factual sub-buckets, no LLM narrative ---
    should_be_on_but_dark = [
        {"account": f.client_name, "detail": f.message}
        for f in flags
        if f.category == FlagCategory.ads_off_should_be_on
    ]

    zero_spend_by_account: dict[str, dict] = {}
    for f in flags:
        if f.category != FlagCategory.ads_off_zero_spend:
            continue
        platform, _, note = f.message.partition(": ")
        entry = zero_spend_by_account.setdefault(f.client_name, {"account": f.client_name, "platforms": [], "note": note})
        entry["platforms"].append(platform)
    campaigns_on_zero_spend = [
        {"account": e["account"], "platform": " + ".join(e["platforms"]), "note": e["note"]}
        for e in zero_spend_by_account.values()
    ]

    unsettled = [
        {"account": f.client_name, "note": f.message} for f in flags if f.category == FlagCategory.ads_off_unsettled
    ]
    verified_off = [
        {"account": f.client_name, "note": f.message}
        for f in flags
        if f.category == FlagCategory.ads_off_verified_off
    ]

    # --- New deals / went live: plain factual lists ---
    new_deals = [{"account": f.client_name, "note": f.message} for f in flags if f.category == FlagCategory.new_deal]
    went_live = [{"account": name} for name in sorted(live_accounts - previous_live_accounts)]

    stat_tiles = {
        "waiting_to_go_live": len(waiting_to_go_live),
        "behind_14_day_target": sum(1 for r in waiting_to_go_live if r["day"] >= _MKTG_BEHIND_DAYS),
        "should_be_on_but_dark": len(should_be_on_but_dark),
        "verified_off": len(verified_off),
        "campaigns_on_zero_spend": len(campaigns_on_zero_spend),
        "website_lane_builds": sum(
            1 for ctx in account_context.values() if ctx.get("package") in _WEBSITE_LANE_PACKAGES
        ),
    }

    return json.dumps(
        {
            "stat_tiles": stat_tiles,
            "waiting_to_go_live": waiting_to_go_live,
            "accounts_chart": accounts_chart,
            "clock_threshold_days": _MKTG_BEHIND_DAYS,
            "ads_off": {
                "should_be_on_but_dark": should_be_on_but_dark,
                "campaigns_on_zero_spend": campaigns_on_zero_spend,
                "unsettled": unsettled,
                "verified_off": verified_off,
            },
            "new_deals": new_deals,
            "went_live": went_live,
            "live_accounts": sorted(live_accounts),
            "narrative_error": narrative_error,
            "narrative_batches": narrative_batches,
        }
    )
