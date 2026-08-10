"""
Thin REST client for the Meta Marketing API (Graph API) — same no-SDK, thin
httpx-client style as google_ads_client.py.

Auth is a permanent System User access token, unlike Google's OAuth refresh
dance — confirmed via /debug_token, 2026-08-06: expires_at=0, i.e. it doesn't
expire/rotate on its own, so there's no token-minting step here at all.

Confirmed against real data, 2026-08-06:
  - Ad account IDs come pre-formatted with the "act_" prefix from Atlas's
    metaAdAccountId field — no parsing needed, unlike Google's dashed
    customer IDs.
  - The insights endpoint only returns a row for a campaign if it had
    activity in the requested window — unlike Google Ads, which returns
    every campaign resource that ever existed, zero-activity or not. A
    campaign with no insights row gets zero metrics here, not omitted from
    the campaigns list (the campaigns list itself is a separate call and
    always includes every campaign regardless of activity).
  - spend/impressions/clicks/cpc come back as numeric STRINGS; ctr comes back
    as a PERCENTAGE string (e.g. "1.025641" for 1.03%), not a fraction like
    Google's ctr — divided by 100 here so the two platforms are comparable.
  - There is no unified "conversions" metric — insights' `actions` field is a
    heterogeneous list of {action_type, value} pairs that varies by campaign
    objective (leads, purchases, messages, engagement, ...) with no single
    action_type that means "conversion" across all of them. Mapping this
    properly needs per-objective business rules this pass doesn't guess at —
    conversions/conversions_value are hardcoded to 0.0 for every Meta
    campaign for now, not computed from `actions` at all.
  - effective_status uses Meta's own vocabulary (ACTIVE/PAUSED/DELETED/
    ARCHIVED/...); ACTIVE is normalized to "ENABLED" here so shared logic
    (filter_relevant_campaigns, classify_ads_off) works identically across
    platforms without a platform-specific branch. Everything else passes
    through as-is.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from adspend.config import get_settings

_API_HOST = "https://graph.facebook.com"

# Meta's documented date_preset values for the subset of our existing
# ALLOWED_DATE_RANGES literals (see google_ads_client.py) that have a direct
# equivalent — anything else (LAST_N_DAYS) is handled via an explicit
# time_range instead, same BETWEEN-style approach as Google's _build_date_clause.
_DATE_PRESETS = {
    "TODAY": "today",
    "YESTERDAY": "yesterday",
    "LAST_7_DAYS": "last_7d",
    "LAST_14_DAYS": "last_14d",
    "LAST_30_DAYS": "last_30d",
    "THIS_MONTH": "this_month",
    "LAST_MONTH": "last_month",
}

_LAST_N_DAYS_RE = re.compile(r"^LAST_(\d+)_DAYS$")


def _build_date_params(date_range: str) -> dict[str, str]:
    if date_range in _DATE_PRESETS:
        return {"date_preset": _DATE_PRESETS[date_range]}
    match = _LAST_N_DAYS_RE.match(date_range)
    if not match:
        raise ValueError(
            f"date_range must be one of {sorted(_DATE_PRESETS)} or match LAST_N_DAYS, got {date_range!r}"
        )
    n = int(match.group(1))
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=n)
    end = today - timedelta(days=1)
    return {"time_range": f'{{"since":"{start.isoformat()}","until":"{end.isoformat()}"}}'}


class MetaAdsClient:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = httpx.Client(timeout=30.0)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Token goes in the Authorization header, NOT as an access_token
        query param, despite that being Graph API's other supported auth
        method — confirmed the hard way, 2026-08-10: httpx's default
        HTTPStatusError.__str__ includes the full request URL, so a
        query-param token leaks in plaintext into every error message this
        raises, which flows straight into ad_platform_errors and
        context_gather_json and gets persisted to the database. The header
        never appears in that string."""
        resp = self._client.get(
            f"{_API_HOST}/{self._settings.meta_api_version}/{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._settings.meta_access_token}"},
        )
        resp.raise_for_status()
        return resp.json()

    def get_account_spend(self, ad_account_id: str, date_range: str = "YESTERDAY") -> dict[str, Any]:
        """Same return shape as GoogleAdsClient.get_account_spend() on purpose
        — lets daily_go_live_audit.py and ads_off_classification.py treat
        both platforms' results identically."""
        date_params = _build_date_params(date_range)

        campaigns_list = self._get(f"{ad_account_id}/campaigns", {
            "fields": "id,name,status,effective_status,objective",
            "limit": 500,
        }).get("data", [])

        insights_rows = self._get(f"{ad_account_id}/insights", {
            "level": "campaign",
            "fields": "campaign_id,spend,impressions,clicks,ctr,cpc",
            **date_params,
        }).get("data", [])
        insights_by_campaign = {row["campaign_id"]: row for row in insights_rows}

        campaigns = []
        totals = {"cost": 0.0, "impressions": 0, "clicks": 0}
        enabled_count = 0
        for c in campaigns_list:
            insight = insights_by_campaign.get(c.get("id"), {})
            cost = float(insight.get("spend", 0))
            impressions = int(float(insight.get("impressions", 0)))
            clicks = int(float(insight.get("clicks", 0)))

            totals["cost"] += cost
            totals["impressions"] += impressions
            totals["clicks"] += clicks

            status = "ENABLED" if c.get("effective_status") == "ACTIVE" else (c.get("effective_status") or c.get("status"))
            if status == "ENABLED":
                enabled_count += 1

            campaigns.append({
                "id": c.get("id"),
                "name": c.get("name"),
                "status": status,
                "channel_type": c.get("objective"),
                "cost": cost,
                "impressions": impressions,
                "clicks": clicks,
                "ctr": float(insight.get("ctr", 0)) / 100 if insight.get("ctr") else 0.0,
                "avg_cpc": float(insight.get("cpc", 0)),
                "conversions": 0.0,
                "cost_per_conversion": 0.0,
                "conversions_value": 0.0,
            })

        return {
            "ad_account_id": ad_account_id,
            "date_range": date_range,
            "total_cost": totals["cost"],
            "total_impressions": totals["impressions"],
            "total_clicks": totals["clicks"],
            "total_conversions": 0.0,
            "total_conversions_value": 0.0,
            "enabled_campaign_count": enabled_count,
            "campaigns": campaigns,
        }
