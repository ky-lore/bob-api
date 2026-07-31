"""
Synthesizes the dashboard's per-account "what's blocking" narrative — the one
piece of the original system that was genuinely LLM-narrated, not a fixed
template (see chat history: sentences like "Client GHOSTING (red list) —
Meta campaign on, $0 spend. Escalation call overdue." require reading raw
signals and writing prose, not applying a rule). Everything numeric (stat
tile counts, day counts, package/stage) stays deterministic Python — this
only synthesizes the sentence, from facts already gathered elsewhere.
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
    "picking a side — never silently resolve a conflict. No greetings, no preamble, no markdown."
)


def synthesize_blocking_narratives(accounts: list[dict[str, Any]]) -> dict[str, str]:
    """accounts: list of {"account": str, "day": int, "stage": str, "context": [str, ...]}
    where context is the already-gathered flag messages/facts for that account.
    Returns {account_name: narrative_sentence}.

    Raises on any failure — deliberately, so the caller (build_dashboard_json)
    can decide how to degrade AND surface why, instead of this silently
    swallowing the error with no visibility anywhere (the first version of
    this did exactly that, and it made a real failure undiagnosable from the
    dashboard alone)."""
    if not accounts:
        return {}

    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    # max_tokens scales with account count — a fixed budget silently truncates
    # the JSON output (and therefore the tool call) once there are enough
    # accounts; 81 real accounts in the first live run would have been tight
    # against a flat 4096.
    max_tokens = min(8192, 300 + 60 * len(accounts))
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
                # A "successful" call that yields nothing usable is just as much
                # a failure as an exception — confirmed the hard way: the first
                # live run with 81 accounts got exactly this (no error, no
                # narratives, every row silently fell back to raw flag text).
                raise RuntimeError(
                    f"Claude tool call returned zero usable narratives for {len(accounts)} accounts "
                    f"(stop_reason={response.stop_reason}, raw narratives array length={len(narratives)})"
                )
            return result

    raise RuntimeError(
        f"Claude response had no {_TOOL_NAME} tool call "
        f"(stop_reason={response.stop_reason}, content block types={[b.type for b in response.content]})"
    )
