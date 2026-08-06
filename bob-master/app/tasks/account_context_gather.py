"""
Full-context gathering for the narrative synthesis, windowed to the last
_CONTEXT_WINDOW_DAYS days (Bob, 2026-08-06) -- the original "no truncation,
full dumps, see how it goes" approach did exactly that: one production run
against all ~133 real Atlas accounts showed the ClickUp folder-walk alone
making 40-90+ comment-fetch calls per account, sequentially, with no
concurrency, reliably exceeding ClickUp's 100 req/min limit and taking
minutes per run. The window is applied BEFORE fetching where possible (skip
a ClickUp task's comment-fetch call entirely if its date_updated is stale),
not just filtered after the fact -- that's what actually relieves the
rate-limit pressure, not just trims context volume.

Two parallel paths:

  - gather_rich_context(): the original MVP path (2026-07-31) — fuzzy-matches
    account name against ClickUp go-live cards / Slack channel names. No
    longer called by the production pipeline (see daily_go_live_audit.py),
    kept and tested as the pre-Atlas fallback shape.
  - gather_atlas_context(): the production path (2026-08-04) — Atlas gives
    exact per-account clickupFolderId / internalSlackChannelId, no matching
    at all.

  - gather_rich_context(): the original MVP path (2026-07-31) — fuzzy-matches
    account name against ClickUp go-live cards / Slack channel names. Still
    what the production pipeline runs today.
  - gather_atlas_context(): the new smoke-test path (2026-08-04) — Atlas (a
    proprietary internal API, now live) gives exact per-account
    clickupFolderId / internalSlackChannelId, no matching at all. Being
    proven out here before it replaces the fuzzy path in daily_go_live_audit.py.

Two sources, both keyed off the account's already-matched go-live card
(gather_rich_context) or Atlas's exact IDs (gather_atlas_context):
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
from datetime import datetime, timedelta, timezone

from app.integrations.clickup import ClickUpClient
from app.integrations.slack import SlackClient
from app.tasks.matching import find_best_match

_INTERNAL_CHANNEL_PREFIX_RE = re.compile(r"^internal-", re.IGNORECASE)

_CONTEXT_WINDOW_DAYS = 7


def _context_cutoff(window_days: int = _CONTEXT_WINDOW_DAYS) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=window_days)


def _is_recent_epoch_ms(value: str | int | None, cutoff: datetime) -> bool:
    """ClickUp's date_updated/date_created/comment date fields are all
    epoch-millisecond strings (confirmed against real task + comment
    payloads, 2026-08-06)."""
    if not value:
        return False
    try:
        return int(value) >= cutoff.timestamp() * 1000
    except (TypeError, ValueError):
        return False

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


def _add_channel_messages(
    slack: SlackClient, channel_id: str, channel_label: str, result: AccountContextResult, cutoff: datetime
) -> None:
    """Shared by both the fuzzy-matched and Atlas-exact-ID Slack paths --
    once a channel_id is in hand (found however), pulling+filtering its
    history is identical. oldest_ts narrows what Slack itself returns (and
    how much it has to paginate through), not just what's kept after the fact."""
    try:
        messages = slack.channel_history(channel_id, oldest_ts=str(cutoff.timestamp()))
    except Exception as exc:
        result.slack_ok = False
        result.slack_error = str(exc)
        result.context.append(f"[Slack context fetch failed for #{channel_label}: {exc}]")
        return

    for m in messages:
        if m.get("subtype") in _SLACK_NOISE_SUBTYPES:
            continue
        text = (m.get("text") or "").strip()
        if text:
            result.context.append(f"[Slack #{channel_label}] {text}")
            result.slack_message_count += 1


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
    _add_channel_messages(slack, match.card_id, match.card_name, result, _context_cutoff())


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


def _add_clickup_folder_context(
    clickup: ClickUpClient, folder_id: str, result: AccountContextResult, cutoff: datetime
) -> None:
    """Atlas's clickupFolderId is a real Folder (Space > Folder > List >
    Task), not a single card -- walks every List inside it, every Task inside
    each List, and every comment on each Task. Confirmed shape against a real
    folder 2026-08-04 (Smithco construction: 2 lists, 13 tasks total)."""
    try:
        lists = clickup.get_folder_lists(folder_id)
    except Exception as exc:
        result.clickup_ok = False
        result.clickup_error = str(exc)
        result.context.append(f"[ClickUp context fetch failed for folder {folder_id}: {exc}]")
        return

    for lst in lists:
        list_id = lst.get("id")
        if not list_id:
            continue
        try:
            task_data = clickup.get_list_tasks(list_id, include_closed=True)
        except Exception as exc:
            result.clickup_ok = False
            result.clickup_error = str(exc)
            result.context.append(f"[ClickUp tasks fetch failed for list {list_id}: {exc}]")
            continue

        for task in task_data.get("tasks", []):
            task_id = task.get("id")
            if not task_id:
                continue
            # Skip the comment-fetch call entirely for a task with no
            # activity in the window -- this is what actually relieves
            # ClickUp's rate limit, not just what trims context after the
            # fact. date_updated moves whenever a comment is added, so a
            # stale-looking old task with a brand-new comment still passes.
            if not _is_recent_epoch_ms(task.get("date_updated") or task.get("date_created"), cutoff):
                continue
            try:
                comments = clickup.get_task_comments(task_id)
            except Exception as exc:
                result.clickup_ok = False
                result.clickup_error = str(exc)
                result.context.append(f"[ClickUp comments fetch failed for task {task_id}: {exc}]")
                continue
            for comment in comments:
                if not _is_recent_epoch_ms(comment.get("date"), cutoff):
                    continue
                text = (comment.get("comment_text") or "").strip()
                if text:
                    result.context.append(f"[ClickUp comment, task {task_id} ({task.get('name')})] {text}")
                    result.clickup_comment_count += 1


def gather_atlas_context(
    clickup_folder_id: str | None,
    slack_channel_id: str | None,
    clickup: ClickUpClient,
    slack: SlackClient,
    window_days: int = _CONTEXT_WINDOW_DAYS,
) -> AccountContextResult:
    """Atlas-exact-ID smoke test path (Bob, 2026-08-04): no fuzzy matching at
    all, no account_name/slack_channels list needed -- both IDs come straight
    from an Atlas account record's `integrations` object. Missing either ID
    just skips that source (ok stays True, counts stay 0), same resilience
    contract as gather_rich_context. window_days (2026-08-06) overrides the
    production default of _CONTEXT_WINDOW_DAYS for one-off pulls (smoke tests
    asking for a different lookback) without needing to monkeypatch the
    module constant."""
    cutoff = _context_cutoff(window_days)
    result = AccountContextResult()
    if clickup_folder_id:
        _add_clickup_folder_context(clickup, clickup_folder_id, result, cutoff)
    if slack_channel_id:
        result.slack_channel_matched = slack_channel_id
        result.slack_match_confidence = "atlas_exact_id"
        result.slack_match_score = 1.0
        _add_channel_messages(slack, slack_channel_id, slack_channel_id, result, cutoff)
    return result
