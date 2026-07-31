"""
MVP "full context" gathering for the waiting-to-go-live narrative synthesis —
per Bob's steer (2026-07-31): prove the logic with the existing fuzzy-match
account correlation *now*, before Atlas (a proprietary internal API, still
unbuilt) provides exact per-account ClickUp folder / Slack channel IDs and
removes the need for fuzzy matching entirely. Deliberately as inefficient as
that implies — no truncation/summarization here, full dumps, see how it goes.

Two sources, both keyed off the account's already-matched go-live card:
  - ClickUp: every comment on the card AND on every one of its subtasks
    ([CLIENT]/[AM] blockers), via get_task_with_subtasks + get_task_comments.
  - Slack: every real message in the account's "internal-<client>" channel
    (system events like channel-join are filtered — see
    _SLACK_NOISE_SUBTYPES, not content for the LLM to interpret), found by
    fuzzy-matching the account name against the channel list (same
    matching.find_best_match machinery already used for ClickUp cards) —
    liberal here on purpose: "ambiguous" confidence still counts (see
    _add_slack_context), unlike the ClickUp/retention board matching, which
    only trusts exact/alias/high. The match's confidence+score are recorded
    on the result either way, so a shaky match is visible, not silently
    trusted as ground truth.

Each external call is soft-fail per account (a bad card ID or an unmatched
Slack channel shouldn't drop the account's other context, or the whole run) —
consistent with the rest of daily_go_live_audit.py's resilience style.

gather_rich_context returns an AccountContextResult rather than a bare list:
`.context` is what actually feeds the narrative LLM; the ok/count/error fields
are diagnostics-only, surfaced separately via the manual-trigger endpoint's
response body (see main.py) so a run's ClickUp/Slack fetch health is visible
without having to grep log lines embedded in the LLM input.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.integrations.clickup import ClickUpClient
from app.integrations.slack import SlackClient
from app.tasks.matching import find_best_match

_INTERNAL_CHANNEL_PREFIX_RE = re.compile(r"^internal-", re.IGNORECASE)

# Slack system-event subtypes -- "<@U0BLXKX8LS1> has joined the channel" and
# friends. Real messages have no "subtype" key at all; these are noise that
# bloats the LLM input without adding anything for it to interpret, and on a
# busy/old channel (real example: a "contreras_welding_shop" channel producing
# 100+ raw lines) can be the difference between a batch fitting its token
# budget or not.
_SLACK_NOISE_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "bot_add", "bot_remove",
}


def extract_channel_client_name(channel_name: str) -> str:
    """Strips the "internal-" prefix SKILL.md documents for new-client
    channels (client-facing twin "advancedmarketers_x_<client>" is not this
    channel and is intentionally not matched here)."""
    return _INTERNAL_CHANNEL_PREFIX_RE.sub("", channel_name)


@dataclass
class AccountContextResult:
    context: list[str] = field(default_factory=list)
    clickup_ok: bool = True
    clickup_comment_count: int = 0
    clickup_error: str | None = None
    slack_channel_matched: str | None = None
    slack_match_confidence: str | None = None
    slack_match_score: float | None = None
    slack_ok: bool = True
    slack_message_count: int = 0
    slack_error: str | None = None


def _add_clickup_context(clickup: ClickUpClient, card_id: str, result: AccountContextResult) -> None:
    try:
        task = clickup.get_task_with_subtasks(card_id)
    except Exception as exc:
        result.clickup_ok = False
        result.clickup_error = str(exc)
        result.context.append(f"[ClickUp context fetch failed for card {card_id}: {exc}]")
        return

    task_ids = [card_id] + [st.get("id") for st in task.get("subtasks", []) if st.get("id")]
    for task_id in task_ids:
        try:
            comments = clickup.get_task_comments(task_id)
        except Exception as exc:
            result.clickup_ok = False
            result.clickup_error = str(exc)
            result.context.append(f"[ClickUp comments fetch failed for task {task_id}: {exc}]")
            continue
        for comment in comments:
            text = (comment.get("comment_text") or "").strip()
            if text:
                result.context.append(f"[ClickUp comment, task {task_id}] {text}")
                result.clickup_comment_count += 1


def _add_slack_context(
    slack: SlackClient, slack_channels: list[dict], account_name: str, result: AccountContextResult
) -> None:
    match = find_best_match(account_name, slack_channels, name_extractor=extract_channel_client_name)
    # Liberal on purpose (Bob, 2026-07-31): "ambiguous" now counts as a match
    # for Slack context, not just exact/alias/high. A wrong Slack channel just
    # means some extra context in one account's LLM input, not a wrong
    # ClickUp-card correlation — much lower stakes than the board-matching use
    # of find_best_match, so the same caution isn't warranted here. Confidence
    # + score are recorded so a bad match is visible, not silently trusted.
    if match.confidence == "none":
        return

    result.slack_channel_matched = match.card_name
    result.slack_match_confidence = match.confidence
    result.slack_match_score = match.score
    try:
        messages = slack.channel_history(match.card_id)
    except Exception as exc:
        result.slack_ok = False
        result.slack_error = str(exc)
        result.context.append(f"[Slack context fetch failed for #{match.card_name}: {exc}]")
        return

    for m in messages:
        if m.get("subtype") in _SLACK_NOISE_SUBTYPES:
            continue
        text = (m.get("text") or "").strip()
        if text:
            result.context.append(f"[Slack #{match.card_name}] {text}")
            result.slack_message_count += 1


def gather_rich_context(
    account_name: str,
    card_id: str | None,
    clickup: ClickUpClient,
    slack: SlackClient,
    slack_channels: list[dict],
) -> AccountContextResult:
    """card_id missing or no confident Slack channel match both just skip that
    source (ok stays True, counts stay 0) — never raises."""
    result = AccountContextResult()
    if card_id:
        _add_clickup_context(clickup, card_id, result)
    _add_slack_context(slack, slack_channels, account_name, result)
    return result
