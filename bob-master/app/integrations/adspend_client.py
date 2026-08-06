"""
Thin REST client for the adspend service (see ../../adspend/README.md) — a
real network call over HTTP, not an in-process import of adspend's code. That
separation is the whole point of adspend being its own service: this
service's credentials (Google Ads dev token, OAuth) never need to live in
bob-master's environment, and adspend stays reusable by other consumers later.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


class AdSpendClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._client = httpx.Client(base_url=settings.adspend_base_url, timeout=30.0)

    def get_spend_by_customer_id(self, customer_id: str, date_range: str = "LAST_7_DAYS") -> dict[str, Any]:
        resp = self._client.get(f"/accounts/{customer_id}/spend", params={"date_range": date_range})
        resp.raise_for_status()
        return resp.json()
