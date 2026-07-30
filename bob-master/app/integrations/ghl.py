"""
Direct REST client for GoHighLevel — there is no MCP connector for this one, per
docs/PROJECT-BRIEF-FOR-NEW-DEV.md §3. Required headers on every call: Authorization,
Version, Accept, and User-Agent. Omitting User-Agent gets a silent 403 from GHL's
WAF — this bit the original prompt-based system and will bite this client too if
that header is ever dropped.
"""
from __future__ import annotations

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
