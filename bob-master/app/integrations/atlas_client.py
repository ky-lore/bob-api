"""
Thin REST client for Atlas — the proprietary internal source-of-truth API
(Bob, 2026-08-04) for per-client state: stage, deadlines, projects, assigned
staff, sales notes, and exact integration IDs (Slack, ClickUp folder, Google
Ads MCC, Meta ad account). Replaces the fuzzy-match account correlation used
everywhere else in this package.

Auth is a static key in the x-api-key header (ATLAS_API_KEY).

Response shape confirmed against the real API 2026-08-04: GET /api/accounts
returns {"count": int, "accounts": [...]}, no pagination — all 133 real
records came back in a single call. Revisit if the account count grows large
enough that this stops being true.

Two Slack-channel fields exist per account — slackChannelId and
internalSlackChannelId. UPDATE 2026-09-18: this package now reads
slackChannelId, not internalSlackChannelId. Originally (2026-08-04)
internalSlackChannelId was the one populated (98/133) and pointed at
SKILL.md's "internal-<client>" channel, all public, auto-joinable via
SlackClient.join_all_public_channels(). Atlas's data has since moved to
populating slackChannelId instead (132/148, internalSlackChannelId now
0/148) — these are the "advancedmarketers_x_<client>"-style client-facing
channels, mostly PRIVATE, which a bot can't self-join; getting them
readable took a one-time bulk conversations.invite pass using a Slack user
token (Bob is a member of all of them), not the public-channel auto-join
path. If this field split changes again, re-verify against a real pull
before assuming either field name — see chat history, 2026-09-17/18.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


class AtlasClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._client = httpx.Client(
            base_url=settings.atlas_base_url,
            headers={"x-api-key": settings.atlas_api_key},
            timeout=30.0,
        )

    def get_all_accounts(self) -> list[dict[str, Any]]:
        resp = self._client.get("/api/accounts")
        resp.raise_for_status()
        return resp.json().get("accounts", [])

    def post_campaigns(self, atlas_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Pushes one platform's campaign summary for one account INTO Atlas
        (2026-09-02) -- the reverse direction of get_all_accounts. One call
        per (account, platform); see app/tasks/atlas_campaign_push.py for the
        payload shape (sample provided by Atlas's team)."""
        resp = self._client.post(f"/api/accounts/{atlas_id}/campaigns", json=payload)
        resp.raise_for_status()
        return resp.json() if resp.content else {}
