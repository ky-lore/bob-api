"""
Direct REST client for GoHighLevel — there is no MCP connector for this one, per
docs/PROJECT-BRIEF-FOR-NEW-DEV.md §3. Required headers on every call: Authorization,
Version, Accept, and User-Agent. Omitting User-Agent gets a silent 403 from GHL's
WAF — this bit the original prompt-based system and will bite this client too if
that header is ever dropped.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.config import get_settings


class GHLClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._location_id = settings.ghl_location_id
        self._client = httpx.Client(
            base_url=settings.ghl_base_url,
            headers={
                "Authorization": f"Bearer {settings.ghl_api_key}",
                "Version": settings.ghl_api_version,
                "Accept": "application/json",
                "User-Agent": settings.ghl_user_agent,
            },
            timeout=30.0,
        )

    def search_opportunities(
        self,
        *,
        pipeline_id: str,
        pipeline_stage_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        resp = self._client.get(
            "/opportunities/search",
            params={
                "location_id": self._location_id,
                "pipeline_id": pipeline_id,
                "pipeline_stage_id": pipeline_stage_id,
                "limit": limit,
            },
        )
        resp.raise_for_status()
        return resp.json().get("opportunities", [])

    def recent_closed_won(self, pipeline_id: str, stage_id: str, *, days: int = 3) -> list[dict[str, Any]]:
        """Closed Won opportunities with lastStageChangeAt in the last N days —
        the reliable catch for same-day signings before Slack channels/Zapier
        notes exist (see SKILL.md GHL ACCESS)."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        opportunities = self.search_opportunities(pipeline_id=pipeline_id, pipeline_stage_id=stage_id)
        out = []
        for opp in opportunities:
            changed_at = opp.get("lastStageChangeAt")
            if changed_at and datetime.fromisoformat(changed_at.replace("Z", "+00:00")).replace(tzinfo=None) >= cutoff:
                out.append(opp)
        return out

    def get_contact(self, contact_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/contacts/{contact_id}")
        resp.raise_for_status()
        return resp.json()

    def get_contact_custom_field(self, contact_id: str, field_id: str) -> str | None:
        contact = self.get_contact(contact_id).get("contact", {})
        for field in contact.get("customFields", []):
            if field.get("id") == field_id:
                return field.get("value")
        return None

    # --- Sales audit reads (2026-10-15) -----------------------------------
    # Conversations/messages need a different API version than opps/contacts
    # (confirmed against the audit prototype), hence the per-call override.
    _CONVERSATIONS_VERSION = "2021-04-15"

    def _get(self, path: str, params: dict[str, Any] | None = None, *, version: str | None = None) -> dict[str, Any]:
        """GET with the 429 backoff the prototype needed -- a few hundred
        contact/conversation calls in a row will trip GHL's burst limit."""
        headers = {"Version": version} if version else None
        resp = None
        for attempt in range(5):
            resp = self._client.get(path, params=params, headers=headers)
            if resp.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def list_opportunities(self, pipeline_id: str, *, page_size: int = 100) -> list[dict[str, Any]]:
        """Every opportunity in a pipeline, all statuses, paginated."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._get("/opportunities/search", {
                "location_id": self._location_id, "pipeline_id": pipeline_id, "limit": page_size, "page": page,
            }).get("opportunities", [])
            out += batch
            if len(batch) < page_size:
                return out
            page += 1

    def list_pipeline_stages(self) -> dict[str, tuple[str, str]]:
        """stage id -> (pipeline name, stage name)."""
        pipelines = self._get("/opportunities/pipelines", {"locationId": self._location_id}).get("pipelines", [])
        return {s["id"]: (p["name"], s["name"]) for p in pipelines for s in p.get("stages", [])}

    def list_users(self) -> dict[str, str]:
        """GHL user id -> display name."""
        users = self._get("/users/", {"locationId": self._location_id}).get("users", [])
        return {u["id"]: (u.get("name") or f"{u.get('firstName', '')} {u.get('lastName', '')}".strip()) for u in users}

    def search_contact(self, query: str) -> dict[str, Any] | None:
        contacts = self._get("/contacts/", {"locationId": self._location_id, "query": query, "limit": 3}).get("contacts", [])
        return contacts[0] if contacts else None

    def contact_call_history(self, contact_id: str) -> list[dict[str, Any]]:
        """Every TYPE_CALL message across the contact's conversations. GHL
        phone calls can duplicate the audio line of a Zoom meeting (see
        tasks/weekly-growth-team-qa-scores/SKILL.md) -- callers score both
        sources together, which double-counts a little rather than
        under-counts; fine for ranking reps, not for billing them."""
        conversations = self._get("/conversations/search", {
            "locationId": self._location_id, "contactId": contact_id,
        }, version=self._CONVERSATIONS_VERSION).get("conversations", [])
        calls: list[dict[str, Any]] = []
        for conv in conversations:
            params: dict[str, Any] = {"limit": 100}
            while True:
                body = self._get(f"/conversations/{conv['id']}/messages", params,
                                 version=self._CONVERSATIONS_VERSION).get("messages", {})
                for m in body.get("messages", []):
                    if m.get("messageType") == "TYPE_CALL":
                        calls.append({k: m.get(k) for k in ("id", "dateAdded", "direction", "status", "userId", "meta")})
                if not body.get("nextPage"):
                    break
                params["lastMessageId"] = body["lastMessageId"]
        return calls

