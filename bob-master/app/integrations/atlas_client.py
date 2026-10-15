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

import logging
import time
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_UNSET: Any = object()


class AtlasWriteError(Exception):
    """A rejected write to Atlas, carrying Atlas's own message so the UI can show
    why (e.g. 422 "No active user matches salesRep ...")."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Atlas {status}: {message}")
        self.status = status
        self.message = message


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

    def get_sales_reps(self) -> list[dict[str, Any]]:
        """The sales roster (2026-10-15): active Atlas users in the sales
        department, [{id, name, email}], via GET /api/users/sales-reps (same
        x-api-key as /api/accounts). This endpoint IS the definition of a
        valid sales rep for the sales audit."""
        resp = self._client.get("/api/users/sales-reps")
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def set_sales_attribution(
        self, atlas_id: str, *, actor: str, sales_rep: Any = _UNSET, referral_partner: Any = _UNSET,
    ) -> dict[str, Any]:
        """Updates an account's sales rep and/or referral partner IN Atlas
        (2026-10-15) via PATCH /api/accounts/:id/sales-attribution -- the one
        deliberately narrow write path the sales audit uses (Atlas is the
        source of truth; the audit's job is to fix it there, not shadow it).

        sales_rep: an Atlas user email or id; None clears. referral_partner:
        a partner NAME (Atlas find-or-creates it); None clears. Omit a field
        to leave it alone. `actor` is the human making the change -- the
        API key has no user, so Atlas records this name in its audit log.

        Atlas side effects worth knowing: assigning a rep also adds them to the
        client's assignedStaff and its projects (i.e. grants access), and a
        previous rep is NOT removed. An unknown rep email is a 422, never a
        silent no-op. Raises AtlasWriteError on any 4xx/5xx; a 404 can mean
        the account is gone OR the endpoint isn't deployed yet."""
        body: dict[str, Any] = {"actor": actor}
        if sales_rep is not _UNSET:
            body["salesRep"] = sales_rep
        if referral_partner is not _UNSET:
            body["referralPartner"] = referral_partner
        resp = self._client.patch(f"/api/accounts/{atlas_id}/sales-attribution", json=body)
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message") or resp.text
            except Exception:
                message = resp.text
            raise AtlasWriteError(resp.status_code, (message or "no message")[:300])
        return resp.json() if resp.content else {}

    # Billing fields the sales audit may write to Atlas (2026-10-15). Deliberately narrow: no parentAccountId / upsell
    # (needs a parent), and introEndsDate is never accepted (Atlas recomputes it from firstChargeDate + introPricing).
    BILLING_WRITE_FIELDS = ("baseAmount", "firstChargeDate", "billingType", "package", "introPricing", "billingDay", "prepaidMonths")

    def set_billing(self, atlas_id: str, changes: dict[str, Any], *, actor: str, expect: dict[str, Any] | None = None) -> dict[str, Any]:
        """Updates billing fields IN Atlas via PATCH /api/accounts/:id/billing (2026-10-15) -- same narrow-write
        philosophy as set_sales_attribution. `changes` maps field -> new value (None clears the field; a field that is
        absent is left alone). `expect` is an optional compare-and-set guard {field: value you believe is current}: if
        Atlas's current value differs it answers 409 and writes NOTHING, so a stale snapshot can never overwrite
        someone's newer edit. Raises AtlasWriteError (carrying Atlas's message) on any 4xx/5xx.
        Returns Atlas's body: {id, companyName, billing, changed, previous}."""
        bad = [k for k in changes if k not in self.BILLING_WRITE_FIELDS]
        if bad:
            raise ValueError(f"not writable via set_billing: {bad}")
        body: dict[str, Any] = {**changes, "actor": actor}
        if expect:
            body["expect"] = expect
        resp = self._client.patch(f"/api/accounts/{atlas_id}/billing", json=body)
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message") or resp.text
            except Exception:
                message = resp.text
            raise AtlasWriteError(resp.status_code, (message or "no message")[:300])
        return resp.json() if resp.content else {}

    def post_campaigns(self, atlas_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Pushes one platform's campaign summary for one account INTO Atlas
        (2026-09-02) -- the reverse direction of get_all_accounts. One call
        per (account, platform); see app/tasks/atlas_campaign_push.py for the
        payload shape (sample provided by Atlas's team)."""
        resp = self._client.post(f"/api/accounts/{atlas_id}/campaigns", json=payload)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def post_admin_tasks(self, atlas_id: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        """Bulk-upserts standup action items (ClickUp tasks tagged `action`)
        for one account into Atlas's AdminTask collection (2026-09-28) --
        same push-direction/reverse-of-get_all_accounts idea as post_campaigns,
        but upserted by clickupTaskId rather than append-only, since a task's
        status/due-date changes over time. See app/tasks/standup_action_items.py
        for the payload shape (one dict per task: clickupTaskId, title,
        assignee, dueDate, status, sourceMeeting, url)."""
        resp = self._client.post(f"/api/accounts/{atlas_id}/admin-tasks", json={"tasks": tasks})
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def post_pulse_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Pushes a full Pulse run into Atlas's PulseRun collection for the
        Command Center (2026-09-28) -- store-and-forward, same posture as
        post_campaigns/post_admin_tasks. Unlike those (called per-account, so
        one account's failure doesn't touch the rest), this is ONE bulk POST
        for the entire run -- losing this single request loses the whole
        run's worth of data, not one account's slice, so it gets real retries
        where the other push methods here don't."""
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._client.post("/api/pulse-runs", json=payload)
                resp.raise_for_status()
                logger.info("post_pulse_run: succeeded on attempt %d/3", attempt + 1)
                return resp.json() if resp.content else {}
            except Exception as exc:
                last_exc = exc
                logger.warning("post_pulse_run: attempt %d/3 failed: %s", attempt + 1, exc)
                if attempt < 2:
                    time.sleep(2**attempt)
        raise last_exc
