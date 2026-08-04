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
internalSlackChannelId — and no record has both populated (confirmed against
real data: 22/133 have the former, 98/133 the latter, zero overlap).
internalSlackChannelId is the one this package wants — SKILL.md's "internal-
<client>" channel, not the client-facing "advancedmarketers_x_<client>" twin.
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
