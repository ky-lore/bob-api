"""
Thin REST client for Atlas, duplicated from ../app/integrations/atlas_client.py
rather than imported — this service is meant to be lifted into its own repo
later, so it shouldn't depend on bob-master's app package being importable.
See that file's docstring for the confirmed real response shape.
"""
from __future__ import annotations

from typing import Any

import httpx

from adspend.config import get_settings


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
