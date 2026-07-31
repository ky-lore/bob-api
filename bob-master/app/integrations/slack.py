"""
Slack client using slack_sdk against a bot token (xoxb).

IMPORTANT GOTCHA — flagged rather than silently worked around: SKILL.md relies on
org-wide Slack search (`slack_search_channels`, `slack search "<client> cancel"`)
via Cowork's MCP connector, which was authenticated as a real user
(bob@advancedmarketers.co). Slack's `search.messages` / `search.channels` API
endpoints only work with a *user* token (xoxp) — a bot token cannot call them at
all, regardless of scopes granted. Two real options, needs a decision before the
cancel-intent and new-channel-detection logic can be ported faithfully:

  1. Provision a user token for Bob (via a Slack app with user-token scopes, or
     an internal integration) instead of / in addition to the bot token.
  2. Restrict this task to channels the bot is a member of and enumerate with
     conversations_list + conversations_history instead of search — misses
     anything outside those channels, which defeats the "search cancel intent
     org-wide" requirement in ACCURACY RULES §2.

Shipping with option 2 silently would quietly break the cancellation safety net
this task exists partly to provide — surface this to Chris/Jaime, don't guess.
"""
from __future__ import annotations

from typing import Any

from slack_sdk import WebClient

from app.config import get_settings


class SlackClient:
    def __init__(self) -> None:
        self._client = WebClient(token=get_settings().slack_bot_token)

    def send_dm(self, user_id: str, text: str) -> dict[str, Any]:
        dm = self._client.conversations_open(users=[user_id])
        channel_id = dm["channel"]["id"]
        return self._client.chat_postMessage(channel=channel_id, text=text)

    def list_channels(self, *, types: str = "public_channel,private_channel") -> list[dict[str, Any]]:
        channels: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            resp = self._client.conversations_list(types=types, cursor=cursor, limit=200)
            channels.extend(resp["channels"])
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return channels

    def channel_history(self, channel_id: str, *, oldest_ts: str | None = None) -> list[dict[str, Any]]:
        """Paginates to the full history (or everything since oldest_ts), not
        just the first page — conversations_history caps at 100/page by
        default. This is a known-channel read (a channel ID already in hand),
        which works fine on a bot token; it's org-wide search that's blocked
        on the user-token decision (see module docstring)."""
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            resp = self._client.conversations_history(channel=channel_id, oldest=oldest_ts, cursor=cursor, limit=200)
            messages.extend(resp["messages"])
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return messages

    def join_channel(self, channel_id: str) -> dict[str, Any]:
        """Requires the channels:join bot scope. Public channels only — Slack has
        no API for a bot to self-join a private channel, see module docstring."""
        return self._client.conversations_join(channel=channel_id)

    def join_all_public_channels(self) -> dict[str, list[str]]:
        """Idempotent — channels the bot is already in are just skipped. Safe to
        re-run any time (e.g. after a new public channel is created)."""
        joined, already_in, failed = [], [], []
        for channel in self.list_channels(types="public_channel"):
            if channel.get("is_member"):
                already_in.append(channel["name"])
                continue
            try:
                self.join_channel(channel["id"])
                joined.append(channel["name"])
            except Exception as exc:
                failed.append(f"{channel['name']}: {exc}")
        return {"joined": joined, "already_in": already_in, "failed": failed}
