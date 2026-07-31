"""
Builds the JSON stored on AuditRun.dashboard_json: deterministic stat-tile
counts (accurate, no hallucination risk) plus an LLM-synthesized "what's
blocking" sentence per flagged account (see integrations/anthropic_client.py).

Not a reproduction of the original dashboard's exact 6 stat tiles — several
of those (e.g. "board live, data dark", "ads off correctly") depend on
stage-aware checks that aren't built yet. These are honest counts of what
this port actually tracks today, not a guess at matching the original
tile-for-tile.
"""
from __future__ import annotations

import json
from collections import defaultdict

from app.integrations.anthropic_client import synthesize_blocking_narratives
from app.models import Flag, FlagCategory


def build_dashboard_json(
    flags: list[Flag],
    account_context: dict[str, dict],
    all_account_names: set[str],
) -> str:
    by_account: dict[str, list[Flag]] = defaultdict(list)
    for f in flags:
        by_account[f.client_name].append(f)

    accounts_for_llm = [
        {
            "account": account_name,
            "day": account_context.get(account_name, {}).get("day", 0),
            "stage": account_context.get(account_name, {}).get("stage", "unknown"),
            "context": [f.message for f in account_flags],
        }
        for account_name, account_flags in by_account.items()
    ]

    narratives = synthesize_blocking_narratives(accounts_for_llm)

    rows = [
        {
            "account": a["account"],
            "day": a["day"],
            "stage": a["stage"],
            "blocking": narratives.get(a["account"]) or "; ".join(a["context"]),
        }
        for a in accounts_for_llm
    ]
    rows.sort(key=lambda r: r["day"], reverse=True)  # oldest first, matching the reference dashboard

    stat_tiles = {
        "flagged_accounts": len(by_account),
        "clock_violations": sum(1 for f in flags if f.category == FlagCategory.clock_violation),
        "heartbeat_mismatches": sum(1 for f in flags if f.category == FlagCategory.heartbeat_mismatch),
        "retention_risks": sum(
            1 for f in flags if f.category == FlagCategory.action_needed and "Retention" in f.message
        ),
        "new_deals": sum(1 for f in flags if f.category == FlagCategory.new_deal),
        "total_accounts_tracked": len(all_account_names),
    }

    return json.dumps({"stat_tiles": stat_tiles, "rows": rows})
