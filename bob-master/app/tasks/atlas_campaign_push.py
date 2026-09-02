"""
Pushes real campaign spend (Google Ads + Meta) INTO Atlas via
POST /api/accounts/{atlas_id}/campaigns -- the reverse direction of
app/tasks/atlas_report.py (which builds a GET response Atlas can pull).
One POST per (account, platform) that has an ID on file, matching the
sample payload Atlas's team provided (2026-09-02):

    {"platform": "google", "adAccountId": ..., "source": "google-ads-sync",
     "capturedAt": <ISO8601 UTC>, "summary": {spend, currency, impressions,
     clicks, conversions, periodStart, periodEnd}, "campaigns": [...]}

Manual-trigger only for now (see app/routers/atlas_campaign_push.py) -- this
is bob-master's first-ever WRITE into a live external system Atlas itself
serves execs from, so it doesn't run on the unattended daily cron yet.

Reporting window is a rolling 30 days ending yesterday (_PUSH_WINDOW_DAYS),
not a calendar month like the sample's Aug 1-31 example -- that specific
range read as illustrative sample data, not a hard requirement, and a
rolling window needs no month-boundary logic. Easy to change if Atlas
actually wants calendar months.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from adspend.google_ads_client import GoogleAdsClient, filter_relevant_campaigns
from adspend.meta_ads_client import MetaAdsClient
from app.integrations.atlas_client import AtlasClient

_PUSH_WINDOW_DAYS = 30


def _period_bounds() -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=_PUSH_WINDOW_DAYS)
    end = today - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _campaign_payload(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": c["id"],
        "name": c["name"],
        "status": c["status"],
        "spend": c["cost"],
        "clicks": c["clicks"],
        "conversions": c["conversions"],
    }


def _build_platform_payload(
    platform: str,
    ad_account_id: str,
    source: str,
    spend: dict[str, Any],
    period_start: str,
    period_end: str,
) -> dict[str, Any]:
    return {
        "platform": platform,
        "adAccountId": ad_account_id,
        "source": source,
        "capturedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {
            "spend": spend["total_cost"],
            "currency": "USD",
            "impressions": spend["total_impressions"],
            "clicks": spend["total_clicks"],
            "conversions": spend["total_conversions"],
            "periodStart": period_start,
            "periodEnd": period_end,
        },
        # Live campaigns + anything with real activity in the window, not the
        # full historical list (see filter_relevant_campaigns) -- same
        # "compressed" reasoning as atlas_report.py's Google-only version.
        "campaigns": [_campaign_payload(c) for c in filter_relevant_campaigns(spend["campaigns"])],
    }


def push_campaign_spend(limit: int | None = None, dry_run: bool = True) -> list[dict[str, Any]]:
    """One result entry per (account, platform) attempted: {"atlas_id",
    "company_name", "platform", "ok", "error", "payload"}. dry_run=True
    (the default -- flip explicitly to push for real) builds every payload
    and skips the actual POST to Atlas, so a first run's output can be
    reviewed safely. limit caps the account universe (smoke-testing), same
    reproducibility convention as atlas_report.py/Settings.debug_max_accounts."""
    atlas_client = AtlasClient()
    atlas_accounts = [a for a in atlas_client.get_all_accounts() if a.get("isActive")]
    if limit is not None:
        atlas_accounts = sorted(atlas_accounts, key=lambda a: a.get("companyName") or "")[:limit]

    google_ads_client = GoogleAdsClient()
    meta_ads_client = MetaAdsClient()
    period_start, period_end = _period_bounds()
    date_range = f"LAST_{_PUSH_WINDOW_DAYS}_DAYS"

    results: list[dict[str, Any]] = []
    for account in atlas_accounts:
        atlas_id = account.get("id")
        company_name = account.get("companyName")
        integ = account.get("integrations") or {}

        for platform, ad_account_id, client, source in (
            ("google", integ.get("googleMccId"), google_ads_client, "google-ads-sync"),
            ("meta", integ.get("metaAdAccountId"), meta_ads_client, "meta-ads-sync"),
        ):
            if not ad_account_id:
                continue

            try:
                spend = client.get_account_spend(ad_account_id, date_range=date_range)
                payload = _build_platform_payload(platform, ad_account_id, source, spend, period_start, period_end)
            except Exception as exc:
                results.append({
                    "atlas_id": atlas_id, "company_name": company_name, "platform": platform,
                    "ok": False, "error": f"spend pull failed: {exc}", "payload": None,
                })
                continue

            if dry_run:
                results.append({
                    "atlas_id": atlas_id, "company_name": company_name, "platform": platform,
                    "ok": True, "error": None, "payload": payload,
                })
                continue

            try:
                atlas_client.post_campaigns(atlas_id, payload)
                results.append({
                    "atlas_id": atlas_id, "company_name": company_name, "platform": platform,
                    "ok": True, "error": None, "payload": payload,
                })
            except Exception as exc:
                results.append({
                    "atlas_id": atlas_id, "company_name": company_name, "platform": platform,
                    "ok": False, "error": f"Atlas POST failed: {exc}", "payload": payload,
                })

    return results
