"""
Thin REST client for the Google Ads API — deliberately NOT the official
`google-ads` Python SDK (grpc-based, heavy dependency tree, its own
google-ads.yaml config convention) to stay consistent with the rest of this
codebase's style: a small httpx wrapper per integration (see
../app/integrations/clickup.py, atlas_client.py). The Ads API has had a REST
surface alongside gRPC since v13-ish; :search covers everything this service
needs so far.

Auth is OAuth2 (refresh token minted once via the Google consent screen,
manually — see adspend/README.md), refreshed to a short-lived access token on
demand and cached in memory until it's within 60s of expiry.

GAQL query fields use snake_case (e.g. metrics.cost_micros); the REST JSON
response keys are camelCase (metrics.costMicros) — standard Google API JSON
convention, not a typo between the two.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from adspend.config import get_settings

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API_HOST = "https://googleads.googleapis.com"

# GAQL's predefined date-range literals for `WHERE segments.date DURING <x>` —
# allow-listed rather than interpolating an arbitrary caller-supplied string
# straight into the query. Google only predefines 7/14/30 -- anything else
# (LAST_10_DAYS, etc.) is handled by _LAST_N_DAYS_RE below instead.
ALLOWED_DATE_RANGES = {
    "TODAY", "YESTERDAY", "LAST_7_DAYS", "LAST_14_DAYS", "LAST_30_DAYS",
    "THIS_MONTH", "LAST_MONTH",
}

# Accepts any "LAST_N_DAYS" a caller asks for (smoke tests keep wanting
# different lookbacks -- L7, L14, L10, ...) by translating it to an explicit
# BETWEEN clause rather than requiring N to be one of Google's predefined
# literals. Matches Google's own LAST_N_DAYS semantic: N days ending
# yesterday, not including today (today's spend is still accruing).
_LAST_N_DAYS_RE = re.compile(r"^LAST_(\d+)_DAYS$")


def _build_date_clause(date_range: str) -> str:
    if date_range in ALLOWED_DATE_RANGES:
        return f"segments.date DURING {date_range}"
    match = _LAST_N_DAYS_RE.match(date_range)
    if not match:
        raise ValueError(
            f"date_range must be one of {sorted(ALLOWED_DATE_RANGES)} or match LAST_N_DAYS, got {date_range!r}"
        )
    n = int(match.group(1))
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=n)
    end = today - timedelta(days=1)
    return f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"


def _digits_only(customer_id: str) -> str:
    return customer_id.replace("-", "").strip()


class GoogleAdsClient:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = httpx.Client(timeout=30.0)
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        resp = self._client.post(
            _TOKEN_URL,
            data={
                "client_id": self._settings.google_ads_client_id,
                "client_secret": self._settings.google_ads_client_secret,
                "refresh_token": self._settings.google_ads_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        self._access_token = body["access_token"]
        self._token_expires_at = time.time() + body.get("expires_in", 3600)
        return self._access_token

    def search(self, customer_id: str, query: str) -> list[dict[str, Any]]:
        """Runs a GAQL query against one customer (client) ID, paginating
        through every page automatically. login-customer-id is always the
        MCC — that's what grants access to a client sub-account under it,
        regardless of which customer_id the query itself targets."""
        customer_id = _digits_only(customer_id)
        headers = {
            "developer-token": self._settings.google_ads_developer_token,
            "login-customer-id": _digits_only(self._settings.google_ads_login_customer_id),
            "Authorization": f"Bearer {self._get_access_token()}",
        }
        url = f"{_API_HOST}/{self._settings.google_ads_api_version}/customers/{customer_id}/googleAds:search"

        results: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            # v21 fixes the page size at 10000 rows and 400s if you try to set
            # it explicitly (confirmed against the real API, 2026-08-06) --
            # pageToken is the only paging knob left.
            body: dict[str, Any] = {"query": query}
            if page_token:
                body["pageToken"] = page_token
            resp = self._client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            payload = resp.json()
            results.extend(payload.get("results", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                return results

    def get_account_spend(self, customer_id: str, date_range: str = "YESTERDAY") -> dict[str, Any]:
        """Per-campaign baseline performance for one customer ID over
        date_range — either one of GAQL's predefined literals or any
        "LAST_N_DAYS" (see _build_date_clause). Google omits a metric field
        entirely from the response when its value is zero/undefined
        (confirmed against real data, 2026-08-06) rather than returning 0, so
        every field below is read with .get(..., 0) — never assume presence.
        Micros fields (cost, avg_cpc, cost_per_conversion) are converted to
        plain currency floats; conversions_value is already in currency
        units, not micros. Field JSON types are inconsistent (int64 fields
        like clicks/impressions come back as numeric strings, but
        cost_per_conversion/average_cpc came back as plain numbers in
        testing) -- float() on everything handles both."""
        date_clause = _build_date_clause(date_range)
        query = (
            "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
            "metrics.cost_micros, metrics.impressions, metrics.clicks, metrics.ctr, "
            "metrics.average_cpc, metrics.conversions, metrics.cost_per_conversion, metrics.conversions_value "
            f"FROM campaign WHERE {date_clause}"
        )
        rows = self.search(customer_id, query)

        campaigns = []
        totals = {"cost": 0.0, "impressions": 0, "clicks": 0, "conversions": 0.0, "conversions_value": 0.0}
        enabled_count = 0
        for row in rows:
            campaign = row.get("campaign", {})
            m = row.get("metrics", {})
            cost = float(m.get("costMicros", 0)) / 1_000_000
            impressions = int(float(m.get("impressions", 0)))
            clicks = int(float(m.get("clicks", 0)))
            conversions = float(m.get("conversions", 0))
            conversions_value = float(m.get("conversionsValue", 0))

            totals["cost"] += cost
            totals["impressions"] += impressions
            totals["clicks"] += clicks
            totals["conversions"] += conversions
            totals["conversions_value"] += conversions_value

            status = campaign.get("status")
            if status == "ENABLED":
                enabled_count += 1
            campaigns.append({
                "id": campaign.get("id"),
                "name": campaign.get("name"),
                "status": status,
                "channel_type": campaign.get("advertisingChannelType"),
                "cost": cost,
                "impressions": impressions,
                "clicks": clicks,
                "ctr": float(m.get("ctr", 0)),
                "avg_cpc": float(m.get("averageCpc", 0)) / 1_000_000,
                "conversions": conversions,
                "cost_per_conversion": float(m.get("costPerConversion", 0)) / 1_000_000,
                "conversions_value": conversions_value,
            })

        return {
            "customer_id": _digits_only(customer_id),
            "date_range": date_range,
            "total_cost": totals["cost"],
            "total_impressions": totals["impressions"],
            "total_clicks": totals["clicks"],
            "total_conversions": totals["conversions"],
            "total_conversions_value": totals["conversions_value"],
            "enabled_campaign_count": enabled_count,
            "campaigns": campaigns,
        }


def filter_relevant_campaigns(campaigns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live (ENABLED) campaigns, plus anything with real activity (cost,
    impressions, or clicks) in the queried date range even if not currently
    enabled -- e.g. a campaign paused mid-window that still spent money.
    A cheap proxy for "recently changed" (Bob, 2026-08-06): a true change-log
    would mean querying Google Ads' separate change_event resource, a real
    extra API call per account -- this costs nothing extra since the activity
    numbers are already in the get_account_spend() result. Drops the long
    tail of years-old REMOVED campaigns with zero activity in range, which is
    what get_account_spend() returns unfiltered (every campaign resource that
    ever existed on the account, not just active ones)."""
    return [c for c in campaigns if c["status"] == "ENABLED" or c["cost"] > 0 or c["impressions"] > 0 or c["clicks"] > 0]
