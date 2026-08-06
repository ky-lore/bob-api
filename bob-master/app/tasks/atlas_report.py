"""
Consolidated per-account report built FOR Atlas to pull, not for bob-master's
own dashboard (see dashboard_summary.py for that) -- Atlas is the master
account DB across the agency, and wants to GET a compressed, structured feed
of {status, recent work, real ad spend} per account, presumably via a daily
cron hitting the endpoint in app/routers/atlas_report.py (Bob, 2026-08-06:
"inefficiency isn't a big deal, it'll run on a daily basis" -- so this
recomputes live on every call, no caching layer here).

Every record carries atlas_id explicitly (Bob: "if we could have the atlas
client IDs be re-passed in that would help") so Atlas can join the response
straight back to its own records without any name-matching on its end.

"Compressed": ad spend is reduced to account-level totals + only the
currently-ENABLED campaigns, not the full historical list including
years-old REMOVED campaigns (see adspend/google_ads_client.py) -- that
history is real, but it's not what Atlas wants to display.
"""
from __future__ import annotations

from typing import Any

from adspend.google_ads_client import GoogleAdsClient
from app.integrations.anthropic_client import synthesize_account_reports
from app.integrations.atlas_client import AtlasClient
from app.integrations.clickup import ClickUpClient
from app.integrations.slack import SlackClient
from app.tasks.account_context_gather import gather_atlas_context
from app.tasks.daily_go_live_audit import _days_since_atlas_created_at


def _compress_google_ads_summary(spend: dict[str, Any]) -> dict[str, Any]:
    return {
        "customer_id": spend["customer_id"],
        "date_range": spend["date_range"],
        "total_cost": spend["total_cost"],
        "total_impressions": spend["total_impressions"],
        "total_clicks": spend["total_clicks"],
        "total_conversions": spend["total_conversions"],
        "total_conversions_value": spend["total_conversions_value"],
        "enabled_campaign_count": spend["enabled_campaign_count"],
        "enabled_campaigns": [c for c in spend["campaigns"] if c["status"] == "ENABLED"],
    }


def build_atlas_report(
    limit: int | None = None,
    context_window_days: int = 10,
    spend_date_range: str = "LAST_10_DAYS",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """limit: cap the account universe for smoke testing (sorted by
    companyName first, same reproducibility convention as
    Settings.debug_max_accounts) -- None means every active Atlas account.

    Returns (records, narrative_batch_results). Each record is one account:
    {atlas_id, company_name, stage, day, is_live, google_ads, status,
    recent_work} -- google_ads is None if the account has no googleMccId or
    the pull failed (soft-failed, never drops the record itself)."""
    atlas_accounts = [a for a in AtlasClient().get_all_accounts() if a.get("isActive")]
    if limit is not None:
        atlas_accounts = sorted(atlas_accounts, key=lambda a: a.get("companyName") or "")[:limit]

    clickup = ClickUpClient()
    slack = SlackClient()
    google_ads_client = GoogleAdsClient()

    records: list[dict[str, Any]] = []
    narrative_inputs: list[dict[str, Any]] = []

    for account in atlas_accounts:
        name = account.get("companyName")
        if not name:
            continue
        integ = account.get("integrations") or {}
        folder_id = integ.get("clickupFolderId") or None
        channel_id = integ.get("internalSlackChannelId") or None
        customer_id = integ.get("googleMccId") or None

        ctx_result = gather_atlas_context(folder_id, channel_id, clickup, slack, window_days=context_window_days)

        google_ads_summary: dict[str, Any] | None = None
        google_ads_error: str | None = None
        if customer_id:
            try:
                spend = google_ads_client.get_account_spend(customer_id, date_range=spend_date_range)
                google_ads_summary = _compress_google_ads_summary(spend)
            except Exception as exc:
                google_ads_error = str(exc)

        is_live = google_ads_summary["total_cost"] > 0 if google_ads_summary else bool(account.get("isActive"))
        day = _days_since_atlas_created_at(account.get("createdAt"))
        stage = account.get("stage") or "unknown"

        records.append({
            "atlas_id": account.get("id"),
            "company_name": name,
            "stage": stage,
            "day": day,
            "is_live": is_live,
            "google_ads": google_ads_summary,
            "google_ads_error": google_ads_error,
        })
        narrative_inputs.append({
            "account": name,
            "day": day,
            "stage": stage,
            "is_live": is_live,
            "context": ctx_result.context,
        })

    reports, batch_results = synthesize_account_reports(narrative_inputs)
    for record in records:
        report = reports.get(record["company_name"], {})
        record["status"] = report.get("status")
        record["recent_work"] = report.get("recent_work")

    return records, batch_results
