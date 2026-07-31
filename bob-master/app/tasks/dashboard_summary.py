"""
Builds the JSON stored on AuditRun.dashboard_json.

Fully macro (Bob, 2026-07-31): every account matched to a go-live-list card
gets ONE row — live or not, whatever package it might be tagged with — with
its day count, live/not-live status, ad spend summary, and an LLM-synthesized
"where they stand" narrative built from its full ClickUp+Slack context (see
account_context_gather.py). This replaces the old package-type clock-threshold
branching (marketing 14d/21d, website 10d, custom-ETA, SEO-same-week, etc.)
entirely — Bob was explicit he's done with that granularity for now and wants
the full-account macro picture first, drilling down into specifics later.

  - stat_tiles: simple deterministic counts (tracked / live / not-live, plus
    the ads-off buckets, unchanged)
  - accounts_overview: every matched account, oldest-signed first, each with
    an LLM narrative synthesized over its full context — the one section that
    gets narrative prose
  - accounts_chart: day count per account, colored live vs not-live
  - ads_off: the 4 sub-buckets from ads_off_classification.py (unchanged —
    already package-agnostic)
  - new_deals / went_live: plain factual lists (unchanged)
  - web_builds: the separate Web Build Pipeline list (website builds, not
    client accounts, outside the 90-day go-live board window), oldest first —
    added 2026-07-31, no threshold/flag logic yet, just visibility
  - live_accounts: stored so the NEXT run can diff against it for went_live
"""
from __future__ import annotations

import json
from collections import defaultdict

from app.integrations.anthropic_client import synthesize_account_narratives
from app.models import Flag, FlagCategory


def _group_flags_by_account(flags: list[Flag]) -> dict[str, list[Flag]]:
    by_account: dict[str, list[Flag]] = defaultdict(list)
    for f in flags:
        by_account[f.client_name].append(f)
    return by_account


def all_matched_accounts(account_context: dict[str, dict]) -> list[str]:
    """Every account with a matched go-live-list card — live or not, whatever
    package (package is no longer tracked at all — see module docstring).
    Exposed standalone so the caller can pre-fetch rich per-account context
    (ClickUp comments, Slack channel history — see account_context_gather.py)
    for this full set before build_dashboard_json runs."""
    return list(account_context.keys())


def build_dashboard_json(
    flags: list[Flag],
    account_context: dict[str, dict],
    all_account_names: set[str],
    live_accounts: set[str],
    previous_live_accounts: set[str],
    rich_context: dict[str, list[str]] | None = None,
    spend_by_account: dict[str, dict] | None = None,
    web_builds: list[dict] | None = None,
) -> str:
    """rich_context: optional {account_name: [context strings, ...]} — full
    ClickUp/Slack material (see account_context_gather.py) for that account's
    LLM narrative input. spend_by_account: optional {account_name: {"Google
    Ads": {"spend": float, "enabled_campaigns": int} | None, "Meta": ... |
    None}} — ad spend summary, also fed to the narrative. web_builds: optional
    [{"card_id", "name", "status", "day"}, ...] from the separate Web Build
    Pipeline list (website builds, not client accounts — outside the 90-day
    go-live board window; see daily_go_live_audit.py). Sorted here, oldest
    first, no threshold/flag logic — still macro, visibility only. Pure merge
    only; this function does no I/O of its own, per the module docstring."""
    by_account = _group_flags_by_account(flags)
    rich_context = rich_context or {}
    spend_by_account = spend_by_account or {}
    web_builds = sorted(web_builds or [], key=lambda r: r.get("day", 0), reverse=True)

    matched_accounts = all_matched_accounts(account_context)
    accounts_for_llm = [
        {
            "account": account_name,
            "day": account_context.get(account_name, {}).get("day", 0),
            "stage": account_context.get(account_name, {}).get("stage", "unknown"),
            "is_live": account_name in live_accounts,
            "ad_spend": spend_by_account.get(account_name, {}),
            "context": [f.message for f in by_account.get(account_name, [])] + rich_context.get(account_name, []),
        }
        for account_name in matched_accounts
    ]

    narrative_error: str | None = None
    narrative_batches: list[dict] = []
    try:
        narratives, narrative_batches = synthesize_account_narratives(accounts_for_llm)
    except Exception as exc:
        # Degrade to the raw context join per account (never blank the
        # dashboard over this), but surface *why* — silently swallowing this
        # is exactly what made the first real failure undiagnosable.
        narratives = {}
        narrative_error = f"{type(exc).__name__}: {exc}"

    accounts_overview = [
        {
            "account": a["account"],
            "day": a["day"],
            "stage": a["stage"],
            "is_live": a["is_live"],
            "ad_spend": a["ad_spend"],
            "status": narratives.get(a["account"]) or "; ".join(a["context"]) or "No additional context gathered.",
        }
        for a in accounts_for_llm
    ]
    accounts_overview.sort(key=lambda r: r["day"], reverse=True)  # oldest first

    # --- Accounts chart: every board-matched account's day count, colored by
    # whether it's currently live — no package-based distinction anymore ---
    accounts_chart = [
        {
            "account": account_name,
            "day": ctx.get("day", 0),
            "status": "live" if account_name in live_accounts else "not_live",
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
        "accounts_tracked": len(matched_accounts),
        "live": sum(1 for a in matched_accounts if a in live_accounts),
        "not_live": sum(1 for a in matched_accounts if a not in live_accounts),
        "should_be_on_but_dark": len(should_be_on_but_dark),
        "verified_off": len(verified_off),
        "campaigns_on_zero_spend": len(campaigns_on_zero_spend),
    }

    return json.dumps(
        {
            "stat_tiles": stat_tiles,
            "accounts_overview": accounts_overview,
            "accounts_chart": accounts_chart,
            "ads_off": {
                "should_be_on_but_dark": should_be_on_but_dark,
                "campaigns_on_zero_spend": campaigns_on_zero_spend,
                "unsettled": unsettled,
                "verified_off": verified_off,
            },
            "new_deals": new_deals,
            "went_live": went_live,
            "web_builds": web_builds,
            "live_accounts": sorted(live_accounts),
            "narrative_error": narrative_error,
            "narrative_batches": narrative_batches,
        }
    )
