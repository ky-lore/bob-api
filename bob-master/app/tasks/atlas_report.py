"""
Consolidated per-account report built FOR Atlas to pull, not for bob-master's
own dashboard (see dashboard_summary.py for that) -- Atlas is the master
account DB across the agency, and wants to GET a compressed, structured feed
of {status, recent work, real ad spend} per account, presumably via a daily
cron hitting the endpoint in app/routers/atlas_report.py (Bob, 2026-08-06:
"inefficiency isn't a big deal, it'll run on a daily basis" -- so this
recomputes live on every call, no caching layer here).

Also doubles as the exec-facing "what's going on with every account this
week" view (2026-09-18, Bob: "execs just be able to know at a glance what is
going on with an account in the past week") -- same GET, same records, just
read directly instead of re-consumed by Atlas. That's why context_window_days
/ spend_date_range default to a week, why both ad platforms are pulled (an
exec thinks in total spend, not per-platform), and why `health` exists: a
single on_track/needs_attention/at_risk read an exec can scan for across a
long list without reading every sentence (see anthropic_client.py's
_HEALTH_VALUES for the exact bar for each).

Every record carries atlas_id explicitly (Bob: "if we could have the atlas
client IDs be re-passed in that would help") so Atlas can join the response
straight back to its own records without any name-matching on its end.

"Compressed": ad spend is reduced to account-level totals + only the
currently-ENABLED campaigns, not the full historical list including
years-old REMOVED campaigns (see adspend/google_ads_client.py) -- that
history is real, but it's not what Atlas wants to display.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from adspend.google_ads_client import GoogleAdsClient
from adspend.meta_ads_client import MetaAdsClient
from app.integrations.anthropic_client import synthesize_account_reports
from app.integrations.atlas_client import AtlasClient
from app.integrations.clickup import ClickUpClient
from app.integrations.slack import SlackClient
from app.models import AccountHealthOverride, AtlasReportRun, ZoomCallRecord
from app.tasks.account_context_gather import gather_atlas_context
from app.tasks.daily_go_live_audit import _days_since_atlas_created_at
from app.tasks.zoom_call_sync import format_transcript_for_context

# Same reasoning as daily_go_live_audit.py's _ZOOM_CONTEXT_CALL_LIMIT -- cap
# regardless of window so an unusually call-heavy account this week can't
# dwarf the rest of that account's context.
_ZOOM_CONTEXT_CALL_LIMIT = 3

# Display-only "recent activity" reference list on each Pulse card (2026-09-21,
# Bob: "tasks active in the last 48hrs of weekdays... referenceable in the
# card under a dropdown"). Does NOT narrow context_window_days -- see
# gather_atlas_context's recent_activity_hours docstring for why the LLM
# still sees the full window regardless of this.
_RECENT_CLICKUP_ACTIVITY_HOURS = 48


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


def _compress_meta_ads_summary(spend: dict[str, Any]) -> dict[str, Any]:
    """Same shape as _compress_google_ads_summary -- MetaAdsClient.get_account_spend
    mirrors GoogleAdsClient's return shape on purpose (see meta_ads_client.py),
    just with ad_account_id instead of customer_id as the platform ID key."""
    return {
        "ad_account_id": spend["ad_account_id"],
        "date_range": spend["date_range"],
        "total_cost": spend["total_cost"],
        "total_impressions": spend["total_impressions"],
        "total_clicks": spend["total_clicks"],
        "total_conversions": spend["total_conversions"],
        "total_conversions_value": spend["total_conversions_value"],
        "enabled_campaign_count": spend["enabled_campaign_count"],
        "enabled_campaigns": [c for c in spend["campaigns"] if c["status"] == "ENABLED"],
    }


def _combined_ad_spend(google_ads: dict[str, Any] | None, meta_ads: dict[str, Any] | None) -> dict[str, Any] | None:
    """Deterministic (not LLM) blended spend across whichever platforms this
    account actually has -- an exec thinks in total dollars and blended cost
    per conversion, not a platform-by-platform split. None if the account has
    neither platform on file (not just a failed pull -- see google_ads_error/
    meta_ads_error on the record for that case)."""
    platforms = [p for p in (google_ads, meta_ads) if p]
    if not platforms:
        return None
    total_spend = sum(p["total_cost"] for p in platforms)
    total_conversions = sum(p["total_conversions"] for p in platforms)
    return {
        "total_spend": total_spend,
        "total_conversions": total_conversions,
        "cost_per_conversion": (total_spend / total_conversions) if total_conversions else None,
    }


def _report(on_progress, payload: dict[str, Any]) -> None:
    """Swallows any error from the callback itself -- progress reporting must
    never be able to break the actual run (same reasoning as job_tracker's
    own try/except around report_progress)."""
    if on_progress is None:
        return
    try:
        on_progress(payload)
    except Exception:
        pass


def _add_zoom_context(db: Session | None, atlas_id: str | None, cutoff: datetime, context: list[str]) -> int:
    """Appends this week's Zoom call transcripts (see zoom_call_sync.py) to
    context in place, same [Zoom call, <topic>, <date>] tag daily_go_live_audit.py
    uses for its LLM input. Filtered by start_time >= cutoff rather than "most
    recent N calls" like the daily audit -- this report covers established
    long-past-due-live accounts too (no _needs_active_monitoring gate), where
    "most recent N" could resurface a months-old call as if it were this
    week's activity. db=None (e.g. a caller with no DB session) or no atlas_id
    just skips Zoom, same soft-fail contract as every other source here.
    Returns the call count for diagnostics visibility."""
    if db is None or not atlas_id:
        return 0
    calls = (
        db.query(ZoomCallRecord)
        .filter(ZoomCallRecord.atlas_account_id == atlas_id, ZoomCallRecord.start_time >= cutoff)
        .order_by(ZoomCallRecord.start_time.desc())
        .limit(_ZOOM_CONTEXT_CALL_LIMIT)
        .all()
    )
    for call in calls:
        call_date = call.start_time.date().isoformat()
        formatted = format_transcript_for_context(call.transcript_text or "")
        context.append(f"[Zoom call, {call.topic}, {call_date}] {formatted}")
    return len(calls)


def _fetch_health_overrides(db: Session | None) -> dict[str, AccountHealthOverride]:
    """One query up front rather than one per account (148 individual
    lookups would be wasteful) -- db=None just skips overrides entirely,
    same soft-fail contract as _add_zoom_context."""
    if db is None:
        return {}
    return {o.atlas_id: o for o in db.query(AccountHealthOverride).all()}


def build_atlas_report(
    db: Session | None = None,
    limit: int | None = None,
    context_window_days: int = 7,
    spend_date_range: str = "LAST_7_DAYS",
    on_progress=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """db: Postgres session for the Zoom transcript lookup (see
    _add_zoom_context) -- optional (defaults to None, which just skips Zoom)
    so callers/tests that don't care about it don't need to wire one up.
    on_progress (2026-09-18, optional): called with a plain dict after each
    account's gather completes ({"phase": "gathering", "completed", "total",
    "account"}), then again per narrative-synthesis batch ({"phase":
    "synthesizing", "completed", "total"}) -- the full run takes ~10+ minutes
    over the real account universe and was otherwise a total black box while
    running (see job_tracker.py). Never lets a progress-reporting error break
    the actual run.
    limit: cap the account universe for smoke testing (sorted by companyName
    first, same reproducibility convention as Settings.debug_max_accounts) --
    None means every active Atlas account. context_window_days/spend_date_range
    default to a week (2026-09-18) to match this report's "what's going on
    with this account in the last week" framing (see module docstring) --
    still overridable for a different lookback.

    Returns (records, narrative_batch_results). Each record is one account:
    {atlas_id, company_name, stage, day, is_live, google_ads, meta_ads,
    ad_spend (combined, deterministic), health, status, recent_work,
    health_overridden, health_override_reason, recent_clickup_activity
    (display-only, last _RECENT_CLICKUP_ACTIVITY_HOURS weekday-hours of
    ClickUp comments -- see gather_atlas_context, never narrows what the
    LLM saw)} -- google_ads/meta_ads are
    None if the account has no ID on file for that platform or the pull
    failed (soft-failed, never drops the record itself; see
    google_ads_error/meta_ads_error to tell the two cases apart). health is
    the LLM's read UNLESS a human has overridden it (see
    AccountHealthOverride) -- health_overridden=True means health is the
    override's value, not the LLM's (kept separately as llm_health)."""
    atlas_accounts = [a for a in AtlasClient().get_all_accounts() if a.get("isActive")]
    if limit is not None:
        atlas_accounts = sorted(atlas_accounts, key=lambda a: a.get("companyName") or "")[:limit]

    clickup = ClickUpClient()
    slack = SlackClient()
    google_ads_client = GoogleAdsClient()
    meta_ads_client = MetaAdsClient()
    zoom_cutoff = datetime.now(timezone.utc) - timedelta(days=context_window_days)

    records: list[dict[str, Any]] = []
    narrative_inputs: list[dict[str, Any]] = []
    total_accounts = len(atlas_accounts)

    for i, account in enumerate(atlas_accounts):
        name = account.get("companyName")
        if not name:
            continue
        atlas_id = account.get("id")
        integ = account.get("integrations") or {}
        folder_id = integ.get("clickupFolderId") or None
        channel_id = integ.get("slackChannelId") or None  # not internalSlackChannelId, see daily_go_live_audit.py
        customer_id = integ.get("googleMccId") or None
        meta_ad_account_id = integ.get("metaAdAccountId") or None

        ctx_result = gather_atlas_context(
            folder_id, channel_id, clickup, slack,
            window_days=context_window_days,
            recent_activity_hours=_RECENT_CLICKUP_ACTIVITY_HOURS,
        )
        zoom_call_count = _add_zoom_context(db, atlas_id, zoom_cutoff, ctx_result.context)

        google_ads_summary: dict[str, Any] | None = None
        google_ads_error: str | None = None
        if customer_id:
            try:
                spend = google_ads_client.get_account_spend(customer_id, date_range=spend_date_range)
                google_ads_summary = _compress_google_ads_summary(spend)
            except Exception as exc:
                google_ads_error = str(exc)

        meta_ads_summary: dict[str, Any] | None = None
        meta_ads_error: str | None = None
        if meta_ad_account_id:
            try:
                meta_spend = meta_ads_client.get_account_spend(meta_ad_account_id, date_range=spend_date_range)
                meta_ads_summary = _compress_meta_ads_summary(meta_spend)
            except Exception as exc:
                meta_ads_error = str(exc)

        # Atlas's own stage, not ad spend on any platform -- same convention
        # daily_go_live_audit.py settled on (see its module docstring): spend
        # can be zero for a live account mid-pause, and an account can't
        # reliably be called "live" just because ONE of two platforms has
        # spend now that both are pulled here.
        is_live = (account.get("stage") or "").lower() == "live"
        day = _days_since_atlas_created_at(account.get("createdAt"))
        stage = account.get("stage") or "unknown"

        records.append({
            "atlas_id": atlas_id,
            "company_name": name,
            "stage": stage,
            "day": day,
            "is_live": is_live,
            "google_ads": google_ads_summary,
            "google_ads_error": google_ads_error,
            "meta_ads": meta_ads_summary,
            "meta_ads_error": meta_ads_error,
            "ad_spend": _combined_ad_spend(google_ads_summary, meta_ads_summary),
            "zoom_call_count": zoom_call_count,
            "recent_clickup_activity": ctx_result.recent_clickup_activity,
        })
        narrative_inputs.append({
            "account": name,
            "day": day,
            "stage": stage,
            "is_live": is_live,
            "context": ctx_result.context,
        })
        _report(on_progress, {"phase": "gathering", "completed": i + 1, "total": total_accounts, "account": name})

    _report(on_progress, {"phase": "synthesizing", "completed": 0, "total": None})
    reports, batch_results = synthesize_account_reports(
        narrative_inputs,
        on_batch_done=lambda done, total: _report(on_progress, {"phase": "synthesizing", "completed": done, "total": total}),
    )
    overrides = _fetch_health_overrides(db)
    for record in records:
        report = reports.get(record["company_name"], {})
        record["health"] = report.get("health") or "on_track"
        record["status"] = report.get("status")
        record["recent_work"] = report.get("recent_work")

        # A human override wins over whatever the LLM inferred THIS run --
        # see AccountHealthOverride's docstring. llm_health is kept alongside
        # so the override doesn't silently hide what the model actually saw.
        override = overrides.get(record["atlas_id"])
        if override:
            record["llm_health"] = record["health"]
            record["health"] = override.health
            record["health_overridden"] = True
            record["health_override_reason"] = override.reason
        else:
            record["health_overridden"] = False
            record["health_override_reason"] = None

    return records, batch_results


def run_and_store_atlas_report(db: Session, limit: int | None = None, on_progress=None) -> AtlasReportRun:
    """Runs build_atlas_report and persists the result as a new AtlasReportRun
    row (2026-09-18) -- the durable counterpart to the manual-trigger
    endpoint's job_tracker status, same split AuditRun already has for the
    daily audit. Always inserts a new row rather than upserting one "latest"
    row, same append-only convention as AuditRun -- cheap to keep every run's
    history (see AtlasReportRun's docstring), and GET .../latest just orders
    by run_at desc. on_progress: see build_atlas_report."""
    records, batch_results = build_atlas_report(db=db, limit=limit, on_progress=on_progress)
    run = AtlasReportRun(
        run_at=datetime.now(timezone.utc),
        limit_used=limit,
        report_json=json.dumps({"count": len(records), "accounts": records, "narrative_batches": batch_results}),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run
