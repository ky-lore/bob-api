"""
Synthesizes the dashboard's per-account status narrative — the one piece of
the original system that was genuinely LLM-narrated, not a fixed template
(see chat history: sentences like "Client GHOSTING (red list) — Meta campaign
on, $0 spend. Escalation call overdue." require reading raw signals and
writing prose, not applying a rule). Everything numeric (stat tile counts,
day counts, live/not-live) stays deterministic Python — this only synthesizes
the sentence, from facts already gathered elsewhere.

Covers EVERY matched go-live-list account, live or not (Bob, 2026-07-31: went
"fully macro" — dropped the old package-type clock-threshold branching
entirely; this now just describes where each account stands given its full
context, rather than only narrating "what's blocking" a not-yet-live subset).

Batched rather than one call for every account: the first live run (81 real
flagged accounts) hit stop_reason=max_tokens with zero usable narratives
produced — a single call's token budget for that many accounts, each with
potentially long context strings (now including full ClickUp comments and
full Slack channel history per account — see account_context_gather.py — so
this budget concern is even more live than when it was first hit), is not a
safe bet. Smaller fixed-size batches keep each call's output comfortably
within budget, and a failed batch only costs that batch's accounts their
narrative (they fall back to raw context text in build_dashboard_json)
instead of taking down the whole run.
"""
from __future__ import annotations

import json
from typing import Any

import anthropic

from app.config import get_settings

_TOOL_NAME = "submit_narratives"
NO_ACTION_NEEDED = "No action needed"
_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": "Submit the synthesized status narrative and recommended action for each account.",
    "input_schema": {
        "type": "object",
        "properties": {
            "narratives": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "account": {"type": "string"},
                        "status": {
                            "type": "string",
                            "description": "One concise, matter-of-fact sentence. No fluff, no greeting.",
                        },
                        "recommended_action": {
                            "type": "string",
                            "description": (
                                "One concrete next step or check-in for the team, based on what's "
                                "missing or stalled in the given context -- e.g. a specific follow-up "
                                "to send, an overdue check-in call, a piece of missing info to chase "
                                "(no ClickUp/Slack activity in a while, an unanswered question, a "
                                "missing ad account ID that should be there by now). Must be something "
                                "someone could actually go do today, not a restatement of the status. "
                                f"Say exactly '{NO_ACTION_NEEDED}' if the account is on track and "
                                "nothing is missing -- never invent an action just to fill this field."
                            ),
                        },
                    },
                    "required": ["account", "status", "recommended_action"],
                },
            }
        },
        "required": ["narratives"],
    },
    # This tool schema is identical on every batch call in a run -- cache it
    # (Bob, 2026-08-11) so only the per-batch account data is billed as fresh
    # input on batches after the first.
    "cache_control": {"type": "ephemeral"},
}

_SYSTEM_PROMPT = (
    "You are synthesizing two things per client account for a marketing agency's internal go-live "
    "tracking dashboard, for management. You'll be given, per account: day count since signing, "
    "whether it is currently live (running ad spend), its Atlas stage, real Google Ads spend data, "
    "and raw context — every ClickUp comment on its card and subtasks, and its Slack channel's "
    "message history. Using ALL of that, produce for EACH account: "
    "(1) status: ONE concise, matter-of-fact sentence describing where the account currently "
    "stands: if it's live, what's actually happening operationally (any risk, any open thread worth "
    "knowing); if it's not live yet, what's blocking it. "
    f"(2) recommended_action: ONE concrete next step someone could go do today, or exactly "
    f"'{NO_ACTION_NEEDED}' if nothing is missing or stalled — see the tool schema for what counts. "
    "Do not invent facts not present in the input. If the input signals conflict with each other, "
    "say so plainly instead of picking a side — never silently resolve a conflict. No greetings, no "
    "preamble, no markdown. You MUST produce one entry per account given, even if it's a short "
    "restatement of the input."
)

# Small on purpose (Bob, 2026-08-11): this only ever runs on an unattended
# daily cron, so there's no reason to trade reliability for wall-clock time.
# A smaller batch gives each account more of the shared max_tokens budget
# and means a bad batch (see module docstring re: the max_tokens incident)
# only costs a handful of accounts their narrative instead of ~20.
_BATCH_SIZE = 5
_TOKENS_PER_ACCOUNT = 150
_MIN_TOKENS = 512
_MAX_TOKENS_CAP = 4096

_REPORT_TOOL_NAME = "submit_account_reports"
_REPORT_TOOL_SCHEMA = {
    "name": _REPORT_TOOL_NAME,
    "description": "Submit the synthesized status + recent-work summary for each account.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reports": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "account": {"type": "string"},
                        "status": {
                            "type": "string",
                            "description": (
                                "One concise, matter-of-fact sentence on where the account currently "
                                "stands. No fluff, no greeting."
                            ),
                        },
                        "recent_work": {
                            "type": "string",
                            "description": (
                                "One to two concise sentences summarizing the concrete work/activity "
                                "reported in the given ClickUp comments and Slack messages -- what the "
                                "team has actually been doing on this account recently (builds, fixes, "
                                "calls, campaign changes, blockers worked). Say 'No recent activity "
                                "reported' if the context has nothing to summarize -- never invent activity."
                            ),
                        },
                    },
                    "required": ["account", "status", "recent_work"],
                },
            }
        },
        "required": ["reports"],
    },
    # Same rationale as _TOOL_SCHEMA's cache_control -- identical on every
    # batch call within a run.
    "cache_control": {"type": "ephemeral"},
}

_REPORT_SYSTEM_PROMPT = (
    "You are synthesizing two things per client account for an internal reporting feed consumed by "
    "another system (Atlas, the agency's master account database), not read directly by a person on a "
    "dashboard. You'll be given, per account: day count since signing, whether it is currently live "
    "(running ad spend), its stage, and raw context — ClickUp comments and Slack channel messages. Using "
    "that, produce for EACH account: "
    "(1) status: one concise, matter-of-fact sentence on where the account currently stands — if live, "
    "what's actually happening operationally (any risk, any open thread worth knowing); if not live, "
    "what's blocking it. "
    "(2) recent_work: one to two concise sentences summarizing the concrete work/activity actually "
    "reported in the given context — what the team has been doing (builds, fixes, calls, campaign "
    "changes, blockers worked), distinct from the status judgment. Say 'No recent activity reported' if "
    "there's nothing to summarize. "
    "Do not invent facts not present in the input. If signals conflict, say so plainly rather than "
    "silently picking a side. No greetings, no preamble, no markdown. You MUST produce one entry per "
    "account given."
)


def _run_in_batches(
    accounts: list[dict[str, Any]], batch_fn
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Shared by synthesize_account_narratives and synthesize_account_reports —
    same batch-size/partial-failure/diagnostics contract either way, only
    batch_fn (and what it returns per account) differs.

    Returns (results, batch_results):
      - results: {account_name: <whatever batch_fn produced for it>} — may be
        a subset of accounts if some batches failed; the caller falls back
        per-account for anything missing.
      - batch_results: one entry per batch attempted — {"batch_index",
        "accounts" (names sent in), "narrated_count", "ok", "error"} — so a
        caller can report exactly which Claude calls succeeded/failed, not
        just an aggregate pass/fail. Surfaced via the manual-trigger endpoint's
        response body (see main.py) for visibility into this batching, which
        the max_tokens incident (see module docstring) showed can silently
        eat whole batches.

    Raises only if EVERY batch fails, so a total outage is still visible
    rather than silently returning {}."""
    if not accounts:
        return {}, []

    results: dict[str, Any] = {}
    batch_results: list[dict[str, Any]] = []
    batch_errors: list[str] = []

    for i in range(0, len(accounts), _BATCH_SIZE):
        batch = accounts[i : i + _BATCH_SIZE]
        batch_index = i // _BATCH_SIZE
        try:
            batch_data = batch_fn(batch)
            results.update(batch_data)
            batch_results.append(
                {
                    "batch_index": batch_index,
                    "accounts": [a["account"] for a in batch],
                    "narrated_count": len(batch_data),
                    "ok": True,
                    "error": None,
                }
            )
        except Exception as exc:
            error_message = str(exc)
            batch_errors.append(f"batch {batch_index + 1} ({len(batch)} accounts): {error_message}")
            batch_results.append(
                {
                    "batch_index": batch_index,
                    "accounts": [a["account"] for a in batch],
                    "narrated_count": 0,
                    "ok": False,
                    "error": error_message,
                }
            )

    if not results and batch_errors:
        raise RuntimeError("; ".join(batch_errors))
    return results, batch_results


def synthesize_account_narratives(
    accounts: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    """accounts: list of {"account": str, "day": int, "stage": str, "is_live": bool,
    "context": [str, ...]} where context is the already-gathered ClickUp/Slack
    material for that account (see account_context_gather.py).

    Returns (narratives, batch_results) — narratives is {account_name:
    {"status": str, "recommended_action": str}} (2026-08-11: added
    recommended_action alongside status, same shape as
    synthesize_account_reports below). See _run_in_batches for the batching
    contract."""
    return _run_in_batches(accounts, _synthesize_batch)


def synthesize_account_reports(
    accounts: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    """Same input shape as synthesize_account_narratives. Returns (reports,
    batch_results) — reports is {account_name: {"status": str, "recent_work":
    str}}, for consumers that want the "what's actually been happening"
    summary as a distinct field rather than folded into one status sentence
    (built for app/tasks/atlas_report.py, 2026-08-06). See _run_in_batches for
    the batching contract."""
    return _run_in_batches(accounts, _synthesize_report_batch)


def _coerce_list(value: Any, key: str | None = None) -> list:
    """Claude occasionally double-encodes a tool-input array field as a JSON
    string instead of a native array -- observed for real, 2026-08-06, in two
    different shapes on the same call: {"reports": "[...]"} (the array
    stringified) and {"reports": "{\\"reports\\": [...]}"} (the ENTIRE object
    stringified and nested one level inside its own key). Iterating a string
    yields characters, not dicts, which crashed with a bare AttributeError
    before this existed.

    Bounded to 3 unwrap attempts (plain list, one level of string-wrapping,
    one level of self-nesting covers every real case seen so far -- this is
    just a safety cap, not a real limit). Anything that still isn't a list
    becomes empty rather than raising here — the caller's "zero usable
    narratives/reports" check turns that into a clear error instead of a
    confusing one."""
    for _ in range(3):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return []
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and key and key in value:
            value = value[key]
            continue
        return []
    return []


def _synthesize_batch(accounts: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    # Two fields per account (status + recommended_action, added 2026-08-11) --
    # same doubled budget as _synthesize_report_batch, same reasoning: don't
    # re-trigger the max_tokens incident this module's docstring warns about.
    max_tokens = min(_MAX_TOKENS_CAP, max(_MIN_TOKENS, _TOKENS_PER_ACCOUNT * 2 * len(accounts)))

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        messages=[{"role": "user", "content": json.dumps(accounts, indent=2)}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            narratives = _coerce_list(block.input.get("narratives", []), key="narratives")
            result = {
                n["account"]: {
                    "status": n.get("status", ""),
                    "recommended_action": n.get("recommended_action") or NO_ACTION_NEEDED,
                }
                for n in narratives
                if isinstance(n, dict) and n.get("account")
            }
            if not result:
                raise RuntimeError(
                    f"zero usable narratives (stop_reason={response.stop_reason}, "
                    f"raw narratives array length={len(narratives)})"
                )
            return result

    raise RuntimeError(
        f"no {_TOOL_NAME} tool call in response "
        f"(stop_reason={response.stop_reason}, content block types={[b.type for b in response.content]})"
    )


def _synthesize_report_batch(accounts: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    # Two fields per account now instead of one -- double the per-account
    # token allowance so this doesn't inherit the max_tokens incident this
    # module's docstring warns about.
    max_tokens = min(_MAX_TOKENS_CAP, max(_MIN_TOKENS, _TOKENS_PER_ACCOUNT * 2 * len(accounts)))

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": _REPORT_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[_REPORT_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": _REPORT_TOOL_NAME},
        messages=[{"role": "user", "content": json.dumps(accounts, indent=2)}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == _REPORT_TOOL_NAME:
            reports = _coerce_list(block.input.get("reports", []), key="reports")
            result = {
                r["account"]: {"status": r.get("status", ""), "recent_work": r.get("recent_work", "")}
                for r in reports
                if isinstance(r, dict) and r.get("account")
            }
            if not result:
                raise RuntimeError(
                    f"zero usable reports (stop_reason={response.stop_reason}, "
                    f"raw reports array length={len(reports)})"
                )
            return result

    raise RuntimeError(
        f"no {_REPORT_TOOL_NAME} tool call in response "
        f"(stop_reason={response.stop_reason}, content block types={[b.type for b in response.content]})"
    )
