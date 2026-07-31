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
_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": "Submit the synthesized status narrative for each account.",
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
                    },
                    "required": ["account", "status"],
                },
            }
        },
        "required": ["narratives"],
    },
}

_SYSTEM_PROMPT = (
    "You are synthesizing a one-line status summary for each client account on a marketing "
    "agency's internal go-live tracking dashboard, for management. You'll be given, per account: "
    "day count since signing, whether it is currently live (running ad spend), its ClickUp card "
    "stage, ad-spend heartbeat data, and raw context — every ClickUp comment on its card and "
    "subtasks, and its Slack channel's message history. Using ALL of that, write ONE concise, "
    "matter-of-fact sentence describing where the account currently stands: if it's live, what's "
    "actually happening operationally (any risk, any open thread worth knowing); if it's not live "
    "yet, what's blocking it. Do not invent facts not present in the input. If the input signals "
    "conflict with each other, say so plainly instead of picking a side — never silently resolve a "
    "conflict. No greetings, no preamble, no markdown. You MUST produce one entry per account "
    "given, even if it's a short restatement of the input."
)

_BATCH_SIZE = 20
_TOKENS_PER_ACCOUNT = 150
_MIN_TOKENS = 512
_MAX_TOKENS_CAP = 4096


def synthesize_account_narratives(
    accounts: list[dict[str, Any]],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """accounts: list of {"account": str, "day": int, "stage": str, "is_live": bool,
    "context": [str, ...]} where context is the already-gathered ClickUp/Slack
    material for that account (see account_context_gather.py).

    Returns (narratives, batch_results):
      - narratives: {account_name: narrative_sentence} — may be a subset of
        accounts if some batches failed; the caller falls back per-account for
        anything missing.
      - batch_results: one entry per batch attempted — {"batch_index",
        "accounts" (names sent in), "narrated_count", "ok", "error"} — so a
        caller can report exactly which Claude calls succeeded/failed, not
        just an aggregate pass/fail. Surfaced via the manual-trigger endpoint's
        response body (see main.py) for visibility into this batching, which
        the max_tokens incident (see module docstring) showed can silently
        eat whole batches.

    Raises only if EVERY batch fails, so a total outage is still visible (via
    build_dashboard_json's narrative_error) rather than silently returning {}."""
    if not accounts:
        return {}, []

    results: dict[str, str] = {}
    batch_results: list[dict[str, Any]] = []
    batch_errors: list[str] = []

    for i in range(0, len(accounts), _BATCH_SIZE):
        batch = accounts[i : i + _BATCH_SIZE]
        batch_index = i // _BATCH_SIZE
        try:
            batch_narratives = _synthesize_batch(batch)
            results.update(batch_narratives)
            batch_results.append(
                {
                    "batch_index": batch_index,
                    "accounts": [a["account"] for a in batch],
                    "narrated_count": len(batch_narratives),
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


def _synthesize_batch(accounts: list[dict[str, Any]]) -> dict[str, str]:
    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    max_tokens = min(_MAX_TOKENS_CAP, max(_MIN_TOKENS, _TOKENS_PER_ACCOUNT * len(accounts)))

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        tools=[_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        messages=[{"role": "user", "content": json.dumps(accounts, indent=2)}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            narratives = block.input.get("narratives", [])
            result = {n["account"]: n["status"] for n in narratives if n.get("account")}
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
