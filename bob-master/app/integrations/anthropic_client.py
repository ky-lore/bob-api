"""
Synthesizes the dashboard's per-account "what's blocking" narrative — the one
piece of the original system that was genuinely LLM-narrated, not a fixed
template (see chat history: sentences like "Client GHOSTING (red list) —
Meta campaign on, $0 spend. Escalation call overdue." require reading raw
signals and writing prose, not applying a rule). Everything numeric (stat
tile counts, day counts, package/stage) stays deterministic Python — this
only synthesizes the sentence, from facts already gathered elsewhere.

Batched rather than one call for every account: the first live run (81 real
flagged accounts) hit stop_reason=max_tokens with zero usable narratives
produced — a single call's token budget for that many accounts, each with
potentially long context strings, is not a safe bet. Smaller fixed-size
batches keep each call's output comfortably within budget, and a failed batch
only costs that batch's accounts their narrative (they fall back to raw flag
text in build_dashboard_json) instead of taking down the whole run.
"""
from __future__ import annotations

import json
from typing import Any

import anthropic

from app.config import get_settings

_TOOL_NAME = "submit_narratives"
_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": "Submit the synthesized 'what's blocking' narrative for each account.",
    "input_schema": {
        "type": "object",
        "properties": {
            "narratives": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "account": {"type": "string"},
                        "blocking": {
                            "type": "string",
                            "description": "One concise, matter-of-fact sentence. No fluff, no greeting.",
                        },
                    },
                    "required": ["account", "blocking"],
                },
            }
        },
        "required": ["narratives"],
    },
}

_SYSTEM_PROMPT = (
    "You are synthesizing a one-line 'what's blocking' status for each client account on a "
    "marketing agency's internal go-live tracking dashboard, for management. Given structured "
    "signals about an account (ClickUp card stage, day count, package type, ad-spend heartbeat "
    "status, retention/cancel-risk flags), write ONE concise, matter-of-fact sentence describing "
    "what's blocking it from going live or what needs attention. Do not invent facts not present "
    "in the input. If the input signals conflict with each other, say so plainly instead of "
    "picking a side — never silently resolve a conflict. No greetings, no preamble, no markdown. "
    "You MUST produce one entry per account given, even if it's a short restatement of the input."
)

_BATCH_SIZE = 20
_TOKENS_PER_ACCOUNT = 150
_MIN_TOKENS = 512
_MAX_TOKENS_CAP = 4096


def synthesize_blocking_narratives(accounts: list[dict[str, Any]]) -> dict[str, str]:
    """accounts: list of {"account": str, "day": int, "stage": str, "context": [str, ...]}
    where context is the already-gathered flag messages/facts for that account.
    Returns {account_name: narrative_sentence} — may be a subset of accounts if
    some batches failed; the caller falls back per-account for anything missing.

    Raises only if EVERY batch fails, so a total outage is still visible (via
    build_dashboard_json's narrative_error) rather than silently returning {}."""
    if not accounts:
        return {}

    results: dict[str, str] = {}
    batch_errors: list[str] = []

    for i in range(0, len(accounts), _BATCH_SIZE):
        batch = accounts[i : i + _BATCH_SIZE]
        try:
            results.update(_synthesize_batch(batch))
        except Exception as exc:
            batch_errors.append(f"batch {i // _BATCH_SIZE + 1} ({len(batch)} accounts): {exc}")

    if not results and batch_errors:
        raise RuntimeError("; ".join(batch_errors))
    return results


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
            result = {n["account"]: n["blocking"] for n in narratives if n.get("account")}
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
