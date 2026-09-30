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
import logging
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
from app.tasks.account_name_matching import normalize
from app.tasks.daily_go_live_audit import _days_since_atlas_created_at
from app.tasks.zoom_call_sync import format_transcript_for_context

logger = logging.getLogger(__name__)

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

# The internal "Team Leads Daily" standup (real Zoom topic: "Daily Leads
# Standup", host chris@ -- confirmed 2026-09-24 against real recordings) is
# team leads walking accounts one by one: campaign pauses, blockers, GBP/ad
# status -- real signal no client-facing source (ClickUp/Slack/the client's
# own Zoom calls) ever captures. Unlike a client-hosted call, this meeting's
# own Zoom topic never names any one client, so zoom_call_sync's topic-based
# fuzzy match can't attribute it -- these transcripts otherwise just sit in
# the unassigned backlog (see app/routers/zoom_review.py). Matched by
# keyword instead, per account, at report time.
_STANDUP_TOPIC = "Daily Leads Standup"
# Cap per account, same reasoning as _ZOOM_CONTEXT_CALL_LIMIT -- a handful
# of accounts get discussed most days; nobody needs more than 2 snippets.
_STANDUP_MENTION_LIMIT = 2
_STANDUP_SNIPPET_CHARS = 500

# Moved here from app/routers/atlas_report.py (2026-09-29) so build_atlas_report
# can filter the account universe by stage BEFORE gathering (see
# app/tasks/cmdctr_report.py) -- the router still uses these for the same
# Pipeline/Live section classification it always has, just imported back
# from here instead of defining them itself; keeping them in the router
# would mean this task module importing FROM the router to reuse them,
# backwards from the router's own existing dependency on this module.
#
# Closed is excluded from Pulse ENTIRELY (Bob: "completely ignored for this
# purpose") -- it's neither pipeline nor an active client, showing it in
# either section is just noise. At Risk is deliberately NOT pipeline: by
# elimination it lands in the Live section below, since an at-risk account
# is presumably a currently-or-recently-live client flagged for churn risk,
# not a pre-launch prospect.
_PIPELINE_STAGES = {"onboarding", "development"}
_EXCLUDED_STAGES = {"closed"}


def _account_stage(a: dict) -> str:
    return (a.get("stage") or "").lower()


def _is_pipeline_stage(a: dict) -> bool:
    return _account_stage(a) in _PIPELINE_STAGES


def _is_excluded_stage(a: dict) -> bool:
    return _account_stage(a) in _EXCLUDED_STAGES


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


def _load_recent_standups(db: Session | None, cutoff: datetime) -> list[tuple[str, str]]:
    """One query + one transcript-cleaning pass for the whole run, not per
    account -- format_transcript_for_context on a handful of standup
    transcripts is cheap; redoing it up to 148 times (once per account in
    build_atlas_report's loop) would not be. Returns (date, cleaned_text)
    pairs, most recent first. db=None skips silently, same soft-fail
    contract as every other source here."""
    if db is None:
        return []
    calls = (
        db.query(ZoomCallRecord)
        .filter(ZoomCallRecord.topic == _STANDUP_TOPIC, ZoomCallRecord.start_time >= cutoff)
        .order_by(ZoomCallRecord.start_time.desc())
        .all()
    )
    return [
        (call.start_time.date().isoformat(), format_transcript_for_context(call.transcript_text or "", max_chars=500_000))
        for call in calls
    ]


def _standup_search_key(company_name: str) -> str | None:
    """The first word of normalize()'d company name, used as a lightweight
    keyword to spot this account being discussed in the standup transcript.
    Best-effort and lossy on purpose -- same posture as the rest of this
    codebase's fuzzy matching (see account_name_matching.py's module
    docstring): a short/generic first word (under 4 chars -- "OC", "LA",
    "LG", ...) is skipped entirely rather than risking false hits across
    unrelated accounts. Real misses (a client discussed by nickname, or a
    company named after its second word) are an accepted gap, not a bug --
    this only ever adds signal on top of the real context sources, never
    replaces them."""
    words = normalize(company_name).split()
    if not words or len(words[0]) < 4:
        return None
    return words[0]


def _add_standup_mentions(company_name: str, standups: list[tuple[str, str]], context: list[str]) -> int:
    """Best-effort: appends up to _STANDUP_MENTION_LIMIT snippets of this
    account being discussed in the internal Daily Leads Standup (see
    _STANDUP_TOPIC's comment above). Tagged with the SAME [Zoom call, ...]
    prefix _add_zoom_context uses -- this genuinely IS Zoom call transcript
    content, just from a different meeting than any client-hosted call, so
    it needs no changes to the evidence-quote schema/UI (still
    source="zoom" if the LLM cites it -- see anthropic_client.py)."""
    key = _standup_search_key(company_name)
    if not key:
        return 0
    key_lower = key.lower()
    added = 0
    for call_date, cleaned in standups:
        if added >= _STANDUP_MENTION_LIMIT:
            break
        idx = cleaned.lower().find(key_lower)
        if idx == -1:
            continue
        start = max(0, idx - _STANDUP_SNIPPET_CHARS // 2)
        end = min(len(cleaned), idx + _STANDUP_SNIPPET_CHARS // 2)
        snippet = cleaned[start:end].strip()
        context.append(f"[Zoom call, {_STANDUP_TOPIC}, {call_date}] …{snippet}…")
        added += 1
    return added


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
    accounts_filter=None,
    batch_size: int | None = None,
    max_tokens_cap: int | None = None,
    system_prompt: str | None = None,
    tool_schema: dict | None = None,
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
    accounts_filter (2026-09-29, optional): a predicate applied to each raw
    Atlas account dict BEFORE gathering (so a filtered-out account never
    pays for a ClickUp/Slack/Zoom/ad-spend pull it doesn't need) -- see
    app/tasks/cmdctr_report.py, which passes _is_pipeline_stage to run this
    over only the onboarding/development account set. Applied before
    `limit`, since limit is a debug cap orthogonal to real filtering.
    batch_size/max_tokens_cap/system_prompt/tool_schema (2026-09-29,
    optional): passed straight through to synthesize_account_reports --
    None means its own defaults (the weekly full-universe run's existing
    behavior, unchanged). See that function's docstring for why a much
    smaller, known-bounded account set (again, CMDCTR) benefits from
    loosening the first two, and needs the latter two overridden as well
    -- a bigger token budget alone doesn't produce deeper output if the
    prompt/schema still explicitly asks for one concise sentence.

    Returns (records, narrative_batch_results). Each record is one account:
    {atlas_id, company_name, stage, day, is_live, google_ads, meta_ads,
    ad_spend (combined, deterministic), health, status, recent_work,
    evidence (0-3 {source, quote} dicts pulled from Slack/Zoom context and
    verified against it -- see _verify_evidence_quotes, empty for a quiet
    account), health_overridden, health_override_reason, recent_clickup_activity
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
    if accounts_filter is not None:
        atlas_accounts = [a for a in atlas_accounts if accounts_filter(a)]
    if limit is not None:
        atlas_accounts = sorted(atlas_accounts, key=lambda a: a.get("companyName") or "")[:limit]

    clickup = ClickUpClient()
    slack = SlackClient()
    google_ads_client = GoogleAdsClient()
    meta_ads_client = MetaAdsClient()
    zoom_cutoff = datetime.now(timezone.utc) - timedelta(days=context_window_days)
    standups = _load_recent_standups(db, zoom_cutoff)

    records: list[dict[str, Any]] = []
    narrative_inputs: list[dict[str, Any]] = []
    total_accounts = len(atlas_accounts)
    logger.info(
        "pulse: starting gather for %d active accounts (limit=%s, %d standup transcripts in window)",
        total_accounts, limit, len(standups),
    )

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
        # zoom_call_count feeds the "N call transcripts this wk" chip in
        # account_pulse.html, which reads as "calls WITH this client" -- so
        # standup mentions (internal chatter ABOUT the client, not a call
        # with them) go straight into the LLM's context below but are
        # deliberately NOT counted here, to avoid that chip lying.
        zoom_call_count = _add_zoom_context(db, atlas_id, zoom_cutoff, ctx_result.context)
        _add_standup_mentions(name, standups, ctx_result.context)

        google_ads_summary: dict[str, Any] | None = None
        google_ads_error: str | None = None
        if customer_id:
            try:
                spend = google_ads_client.get_account_spend(customer_id, date_range=spend_date_range)
                google_ads_summary = _compress_google_ads_summary(spend)
            except Exception as exc:
                google_ads_error = str(exc)
                logger.warning("pulse: google ads pull failed for %s (%s): %s", name, customer_id, exc)

        meta_ads_summary: dict[str, Any] | None = None
        meta_ads_error: str | None = None
        if meta_ad_account_id:
            try:
                meta_spend = meta_ads_client.get_account_spend(meta_ad_account_id, date_range=spend_date_range)
                meta_ads_summary = _compress_meta_ads_summary(meta_spend)
            except Exception as exc:
                meta_ads_error = str(exc)
                logger.warning("pulse: meta ads pull failed for %s (%s): %s", name, meta_ad_account_id, exc)

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
        logger.info("pulse: gathered %d/%d — %s", i + 1, total_accounts, name)

    logger.info("pulse: gather complete, %d accounts; starting synthesis", len(records))
    _report(on_progress, {"phase": "synthesizing", "completed": 0, "total": None})
    # Only forwarded when actually overridden -- letting synthesize_account_reports's
    # own defaults (_BATCH_SIZE/_MAX_TOKENS_CAP/_REPORT_SYSTEM_PROMPT/
    # _REPORT_TOOL_SCHEMA) be the single source of truth for the weekly
    # full-universe run's unchanged behavior.
    synth_kwargs: dict[str, Any] = {}
    if batch_size is not None:
        synth_kwargs["batch_size"] = batch_size
    if max_tokens_cap is not None:
        synth_kwargs["max_tokens_cap"] = max_tokens_cap
    if system_prompt is not None:
        synth_kwargs["system_prompt"] = system_prompt
    if tool_schema is not None:
        synth_kwargs["tool_schema"] = tool_schema

    reports, batch_results = synthesize_account_reports(
        narrative_inputs,
        on_batch_done=lambda done, total: _report(on_progress, {"phase": "synthesizing", "completed": done, "total": total}),
        **synth_kwargs,
    )
    overrides = _fetch_health_overrides(db)
    for record in records:
        report = reports.get(record["company_name"], {})
        record["health"] = report.get("health") or "on_track"
        record["status"] = report.get("status")
        record["recent_work"] = report.get("recent_work")
        record["evidence"] = report.get("evidence") or []

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

    failed_batches = sum(1 for b in batch_results if not b["ok"])
    logger.info(
        "pulse: build complete, %d accounts, %d/%d narrative batches ok",
        len(records), len(batch_results) - failed_batches, len(batch_results),
    )
    return records, batch_results


def _pulse_run_payload(run: AtlasReportRun, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Builds what actually gets pushed to Atlas's Command Center (2026-09-28)
    -- distinct from what Bob keeps for itself in report_json only in that
    narrative_batches is never included (Bob-internal batch diagnostics, not
    part of `records` in the first place). Everything else -- including
    `enabled_campaigns` on google_ads/meta_ads and the full
    recent_clickup_activity list -- is pushed through UNTRIMMED (changed
    2026-09-28, per Chris: "complete, full pulse visibility stored in here").
    This is a deliberate reversal of this function's original trim-before-push
    design: Command Center's meeting-mode focus view reads campaign-level ad
    detail straight off the stored PulseRun now, instead of a second live
    fetch to Atlas's own CampaignSnapshot -- Atlas is still store-and-forward
    (no reshaping/validation on ITS end), it's just storing the whole thing."""
    return {
        "runId": run.id,
        "runAt": run.run_at.isoformat(),
        "count": len(records),
        "accounts": records,
    }


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
    logger.info("pulse: run %s persisted (%d accounts)", run.id, len(records))

    # Push this run into Atlas's Command Center (2026-09-28) -- soft-fail,
    # same posture as every other external push in this codebase: an Atlas
    # outage (or the retries in post_pulse_run exhausting) must never break
    # Bob's own Pulse run, which already succeeded and is stored above.
    # pulse_push (2026-09-29) records the outcome back onto the stored run so
    # it's visible via GET .../latest -- a bare swallowed exception here left
    # a failed push completely invisible; this repo has no Railway log access
    # from a dev session, so this is the only way to see a real error message
    # after the fact instead of just re-guessing at the cause.
    pulse_push_ok = True
    pulse_push_error: str | None = None
    try:
        AtlasClient().post_pulse_run(_pulse_run_payload(run, records))
        logger.info("pulse: run %s pushed to Atlas Command Center", run.id)
    except Exception as exc:
        pulse_push_ok = False
        pulse_push_error = str(exc)
        logger.exception("pulse: run %s Command Center push failed", run.id)

    data = json.loads(run.report_json)
    data["pulse_push"] = {"ok": pulse_push_ok, "error": pulse_push_error}
    run.report_json = json.dumps(data)
    db.commit()
    db.refresh(run)

    return run
