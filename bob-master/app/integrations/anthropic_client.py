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

    Falls back to a deterministic join of the context strings if the API call
    fails for any reason — the dashboard must never go blank because of this
    optional synthesis step."""
    if not accounts:
        return {}

    try:
        settings = get_settings()
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=4096,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            messages=[{"role": "user", "content": json.dumps(accounts, indent=2)}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == _TOOL_NAME:
                narratives = block.input.get("narratives", [])
                return {n["account"]: n["blocking"] for n in narratives if n.get("account")}
    except Exception:
        pass

    return {a["account"]: "; ".join(a.get("context", [])) or "No details available" for a in accounts}
