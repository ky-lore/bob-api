"""
Port of tasks/daily-go-live-audit/SKILL.md.

FULLY MACRO as of 2026-07-31 (Bob's explicit call): the original package-type
clock-threshold branching (marketing 14d/21d, website 10d, custom-ETA,
SEO-same-week, etc. — SKILL.md's PACKAGE CLOCKS section) has been ripped out
entirely. No more per-package day thresholds, no more "exempt" packages, no
more package identification gating anything.

ATLAS-ONLY as of 2026-08-06 (Bob's explicit call): the heartbeat Google Sheets
pull has been dropped ENTIRELY — no reading, no parsing, no fuzzy-matching
against it, nothing. It was the one heartbeat-shaped source left standing
after the 2026-08-04 Atlas migration, and Bob's call is that it's simply
broken data and not worth reconciling against. That also means the one fuzzy
match this module still ran (heartbeat account names -> Atlas companyName) is
gone too — there is now zero fuzzy-matching left anywhere in this pipeline.
Atlas is the entire account universe, and every per-account fact — stage, day
count, exact ClickUp/Slack IDs, the go-live deadline — comes from Atlas by ID,
never by name. Same call extends to the retention-pipeline cancel-intent
cross-check (ClickUp board, fuzzy-matched by name) — dropped for the same
reason: "assume Atlas will always have perfect data regarding account
status," so a second, lower-fidelity system re-verifying what Atlas already
reports (isActive, stage) is no longer worth carrying.

IS_LIVE FROM ATLAS, NOT SPEND (2026-08-06, revised same day): `is_live` is
Atlas's own `stage == "live"`, not ad spend on any platform. Bob's reasoning:
not every client needs Google or Meta spend to be considered live — some
run one platform, some run both, some are website-only and run neither —
so spend presence was never a reliable live/not-live signal, and never will
be once Meta is wired in either. Real spend (Google now, Meta pending) is
still gathered and shown per account — it's cross-check/context data, not
what determines is_live.

GO-LIVE TARGET CLOCK (2026-08-06): "behind the 14-day target" no longer means
a uniform, code-computed 14 days from signing — it's Atlas's own
`deadlines.goLive` date for that specific account (confirmed against real
data: 137/137 active accounts have this populated). An account past that date
and not yet live is "behind"; within _APPROACHING_BUFFER_DAYS of it and not
live is "approaching"; otherwise "on_track". Package/project type
(`projects: [{type, status}, ...]` per Bob) plays no role in this at all —
deliberately not gating the clock on it, even though Atlas may start
returning that array.

Billing/payment status (the reference dashboard's "Dark because payment
UNSETTLED" bucket) is a placeholder only (Bob, 2026-08-06: "that'll take a
while to reconcile") — see AdsOffClassification.billing_unsettled in
ads_off_classification.py. It always reports False; no real signal feeds it
yet (no GHL billing field, no Google Ads billing-status query wired in).

What's fully implemented below (deterministic, spec'd precisely in the prompt,
or built against real sandboxed ClickUp/Atlas data — see chat history):
  - Atlas account correlation (stage, day-count, exact Slack/ClickUp IDs,
    go-live deadline) — see app/integrations/atlas_client.py
  - digest assembly from persisted flags, and AuditRun/Flag persistence
  - full-context gather (full ClickUp folder — every List's every Task's
    comments — + full Slack channel history) via exact Atlas IDs — see
    app/tasks/account_context_gather.py's gather_atlas_context. Skipped for
    accounts that don't need active monitoring (see
    _needs_active_monitoring, 2026-08-11) — an established, long-past-due
    live account doesn't need this daily; ad spend + ads-off classification
    still run for it regardless.
  - auto-joins every public Slack channel the bot isn't already in before
    gathering — channel_history() silently fails on a channel the bot hasn't
    joined, exact ID or not. Private channels still need a manual bot invite;
    there's no Slack API for a bot to self-join one.
  - real Google Ads + Meta spend via adspend/ (see adspend/README.md,
    meta_ads_client.py) — a self-contained package mounted at /adspend on
    this same app (app/main.py) and called in-process here (no self-HTTP hop
    — it's the same process). Spend/campaign data only, not is_live (see
    above) — an account can be Google-only, Meta-only, both, or neither
    (website-only) with no effect on whether it counts as live. Meta's
    System User token, as of 2026-08-06, only has actual access granted to
    5 of the 56 Atlas-mapped Meta ad accounts — the other 51 soft-fail
    (meta_ads_live_ok=False) until access is broadened in Business Manager,
    same as any other per-account external-call failure here.

What's intentionally left as TODOs — these require judgment calls this port
should not guess at:
  - the GHL Closed-Won -> new ClickUp card flow, including reading sales notes
    for the "over-promise" check (Atlas's own salesNotes field may make this
    moot — not yet wired into the narrative context)
  - stage-aware checks that require correlating ClickUp card state with Slack
    channel activity (needs the org-wide search decision — see integrations/slack.py)
  - real billing/payment status (see placeholder note above)
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from adspend.google_ads_client import GoogleAdsClient, filter_relevant_campaigns
from adspend.meta_ads_client import MetaAdsClient
from app.config import get_settings
from app.integrations.atlas_client import AtlasClient
from app.integrations.clickup import ClickUpClient
from app.integrations.ghl import GHLClient
from app.integrations.slack import SlackClient
from app.models import AuditRun, Flag, FlagCategory, FlagSeverity, RunStatus
from app.tasks.account_context_gather import gather_atlas_context
from app.tasks.ads_off_classification import classify_ads_off
from app.tasks.clickup_correlation import resolve_day_count
from app.tasks.dashboard_summary import all_matched_accounts, build_dashboard_json

# An account not yet live, within this many days of its Atlas goLive deadline,
# is "approaching" rather than "on_track" or "behind" -- a starting heuristic
# (Bob hasn't specified an exact buffer), easy to tune later.
_APPROACHING_BUFFER_DAYS = 3


def _days_since_atlas_created_at(created_at: str | None) -> int:
    """Atlas's createdAt is the true origin date (Bob, 2026-08-04). Manual
    .replace("Z", "+00:00") rather than relying on fromisoformat's native "Z"
    handling, which only exists from Python 3.11."""
    if not created_at:
        return 0
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    now = datetime.now(created.tzinfo) if created.tzinfo else datetime.utcnow()
    return (now - created).days


def _go_live_target_status(go_live_deadline: str | None, is_live: bool) -> str:
    """"behind"/"approaching"/"on_track" against Atlas's own deadlines.goLive
    for this account -- not a uniform recomputed 14 days (Bob, 2026-08-06).
    Package/project type never gates this. is_live is Atlas's own stage now
    (2026-08-06), not ad spend -- some clients don't run any ads at all
    (website-only), so spend was never a reliable live signal. "unknown" if
    Atlas has no deadline on file (shouldn't happen given 100% coverage
    confirmed against real data, but never raise over it)."""
    if is_live:
        return "live"
    if not go_live_deadline:
        return "unknown"
    try:
        deadline = datetime.fromisoformat(go_live_deadline.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    now = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now(timezone.utc)
    if now > deadline:
        return "behind"
    if (deadline - now).days <= _APPROACHING_BUFFER_DAYS:
        return "approaching"
    return "on_track"


_STALE_LIVE_MONITORING_WINDOW_DAYS = 14


def _needs_active_monitoring(go_live_deadline: str | None, is_live: bool) -> bool:
    """The full-context gather (ClickUp comments, Slack channel history, LLM
    narrative synthesis) is the real runtime cost driver on a full-account-
    universe run (Bob, 2026-08-11: ~20 min across ~137 accounts) -- NOT the
    ad-spend pulls or ads-off classification, which stay cheap per-account
    REST calls and keep running for every live account regardless of this
    check (see the gather loop and classify_ads_off below), since that's
    exactly what catches an established client's ads silently going dark.

    Not-yet-live accounts always need the full treatment -- that's the whole
    point of this board. A live account only needs it for a short window
    right after going live (to catch early post-launch issues); once it's
    been live more than _STALE_LIVE_MONITORING_WINDOW_DAYS past its Atlas
    go-live deadline, it's established/stable and this returns False --
    the caller skips the expensive gather for it AND drops it from the
    accounts overview/chart/narrative (see build_dashboard_json's
    account_context param).

    A missing/unparseable deadline never excludes an account -- same
    never-assume-when-Atlas-data-is-missing posture as
    _go_live_target_status."""
    if not is_live:
        return True
    if not go_live_deadline:
        return True
    try:
        deadline = datetime.fromisoformat(go_live_deadline.replace("Z", "+00:00"))
    except ValueError:
        return True
    now = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now(timezone.utc)
    return (now - deadline).days <= _STALE_LIVE_MONITORING_WINDOW_DAYS


def build_digest(flags: list[Flag]) -> str:
    """Assembles the digest per SKILL.md DO #6. Caps section length loosely
    toward the ~40-line target — trim further once real flag volume is known."""
    if not flags:
        return "Go-live audit: all clear today. :white_check_mark:"

    sections = [
        (":rotating_light: Action needed today", FlagCategory.action_needed),
        (":credit_card: Payment", FlagCategory.payment),
        (":new: New deals", FlagCategory.new_deal),
        (":white_check_mark: Went live", FlagCategory.went_live),
    ]
    lines = []
    for title, category in sections:
        items = [f for f in flags if f.category == category]
        if not items:
            continue
        lines.append(f"*{title}*")
        for f in items:
            suffix = " (unverified)" if f.unverified else ""
            lines.append(f"• {f.client_name}: {f.message}{suffix}")
    return "\n".join(lines)


def run_daily_go_live_audit(db: Session) -> AuditRun:
    settings = get_settings()
    run_date = datetime.utcnow().date()

    # Re-running the same day (manual trigger during testing, or a legitimate
    # same-day re-run in prod after fixing something) replaces that day's row
    # rather than erroring on the unique constraint — one row per day is a
    # deliberate design choice for the dashboard's history view, but that
    # shouldn't mean a day is stuck with a bad first run forever.
    existing = db.query(AuditRun).filter_by(run_date=run_date).first()
    if existing:
        db.delete(existing)
        db.flush()

    # For "went live" detection: the most recent run strictly before today,
    # not affected by whether today's row already exists from an earlier
    # same-day re-run. live_accounts from its stored dashboard_json is the
    # comparison baseline — diffed against today's live_accounts below.
    previous_run = db.query(AuditRun).filter(AuditRun.run_date < run_date).order_by(AuditRun.run_date.desc()).first()
    previous_live_accounts: set[str] = set()
    if previous_run and previous_run.dashboard_json:
        try:
            previous_live_accounts = set(json.loads(previous_run.dashboard_json).get("live_accounts", []))
        except (ValueError, TypeError):
            pass

    run = AuditRun(run_date=run_date, started_at=datetime.utcnow(), status=RunStatus.partial)
    db.add(run)
    db.flush()  # get run.id before attaching flags

    clickup = ClickUpClient()
    ghl = GHLClient()
    flags: list[Flag] = []
    notes: list[str] = []

    # --- Atlas account universe (2026-08-06: the ONLY account universe now —
    # no heartbeat sheet, no fuzzy matching, nothing else feeds this). ---
    try:
        atlas_accounts = [a for a in AtlasClient().get_all_accounts() if a.get("isActive")]
    except Exception as exc:
        atlas_accounts = []
        notes.append(f"Atlas accounts pull failed: {exc}")

    # DEBUG cap (Bob, 2026-08-06, temporary — see Settings.debug_max_accounts):
    # random, not sorted-first-N -- an alphabetical slice kept showing the
    # same handful of accounts every debug run instead of a representative mix.
    if settings.debug_max_accounts is not None and len(atlas_accounts) > settings.debug_max_accounts:
        atlas_accounts = random.sample(atlas_accounts, settings.debug_max_accounts)
        notes.append(
            f"DEBUG: capped to a random {settings.debug_max_accounts} of the full Atlas account list "
            "(Settings.debug_max_accounts) — not a real run, remove the cap when done debugging"
        )

    all_account_names: set[str] = set()
    account_context: dict[str, dict] = {}  # companyName -> {day, stage, atlas_id, clickup_folder_id, slack_channel_id, google_ads_customer_id, go_live_deadline}
    # is_live comes from Atlas's own stage now (2026-08-06, Bob's explicit
    # call), NOT from ad spend on either platform -- some clients don't run
    # Google or Meta at all (website-only), so spend presence was never a
    # reliable live/not-live signal to begin with. Spend data still gets
    # gathered and shown per account below; it just no longer gates this.
    live_accounts: set[str] = set()

    for atlas_account in atlas_accounts:
        company_name = atlas_account.get("companyName")
        if not company_name:
            continue
        all_account_names.add(company_name)
        if (atlas_account.get("stage") or "").lower() == "live":
            live_accounts.add(company_name)
        integ = atlas_account.get("integrations") or {}
        account_context[company_name] = {
            "day": _days_since_atlas_created_at(atlas_account.get("createdAt")),
            "stage": atlas_account.get("stage") or "unknown",
            "atlas_id": atlas_account.get("id"),
            "clickup_folder_id": integ.get("clickupFolderId") or None,
            "slack_channel_id": integ.get("internalSlackChannelId") or None,
            # Despite the field's name, this is the client's own Google Ads
            # customer ID, not a second per-client MCC — confirmed against
            # real Atlas data 2026-08-06 (e.g. distinct IDs per client, none
            # matching the shared MCC in adspend's GOOGLE_ADS_LOGIN_CUSTOMER_ID).
            "google_ads_customer_id": integ.get("googleMccId") or None,
            # Comes pre-formatted with the "act_" prefix already (confirmed
            # against real Atlas data, 2026-08-06) — no parsing needed.
            "meta_ad_account_id": integ.get("metaAdAccountId") or None,
            "go_live_deadline": (atlas_account.get("deadlines") or {}).get("goLive") or None,
        }

    # --- Web Build Pipeline sweep (Bob, 2026-07-31): a separate ClickUp list
    # tracking website builds, not client accounts -- outside the 90-day
    # go-live board window entirely (real example the original dashboard
    # named: ColdRiite Walk-Ins sitting 374 days). Still macro, no >30-day
    # threshold/flag rule yet -- just card name/status/day count, oldest
    # first. Untouched by the heartbeat removal -- never used heartbeat data. ---
    try:
        web_build_data = clickup.get_list_tasks(settings.clickup_web_build_list_id, include_closed=True)
        web_builds = [
            {
                "card_id": t.get("id"),
                "name": t.get("name"),
                "status": ((t.get("status") or {}).get("status") or "").lower(),
                "day": resolve_day_count(t.get("name") or "", t["date_created"]) if t.get("date_created") else 0,
            }
            for t in web_build_data.get("tasks", [])
        ]
    except Exception as exc:
        web_builds = []
        notes.append(f"Web Build Pipeline pull failed: {exc}")

    # --- GHL Closed Won sweep — data pull wired, card creation is not ---
    try:
        recent_deals = ghl.recent_closed_won(
            settings.ghl_adv_master_pipeline_id, settings.ghl_closed_won_stage_id, days=3
        )
        for deal in recent_deals:
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.new_deal,
                    severity=FlagSeverity.info,
                    client_name=deal.get("name", "unknown"),
                    message="Closed Won in GHL, last 3 days — verify signed agreement and create/confirm ClickUp card",
                    unverified=True,
                    created_at=datetime.utcnow(),
                )
            )
        # TODO(port): read sales notes doc (ghl_field_sales_notes_doc) for the
        # over-promise check (ACCURACY RULES §4) and create the ClickUp card
        # (SKILL.md DO #3) when there's no matching card yet.
    except Exception as exc:
        notes.append(f"GHL Closed Won sweep failed: {exc}")

    # TODO(port): stage-aware checks (DO #4) — needs ClickUp card status + last
    # ~3 days of the matching Slack channel. Blocked on the Slack search-API
    # decision documented in integrations/slack.py.

    slack = SlackClient()

    # --- Auto-join every public channel the bot isn't already in (Bob,
    # 2026-07-31): channel_history() 404s/errors on a channel the bot hasn't
    # joined, silently starving the narrative of Slack context no matter how
    # good the name match is. Idempotent (see slack.py) — cheap to run every
    # day. Private channels still can't be self-joined (no Slack API for
    # that); those need a manual invite, which is why this is a note, not
    # a silent no-op. ---
    try:
        join_result = slack.join_all_public_channels()
        failed = join_result["failed"]
        if join_result["joined"] or failed:
            # Cap the detail: a real run against this workspace produced
            # hundreds of failure lines (mostly rate-limiting before the
            # retry handler was added) that once blew run.notes way past
            # anything readable. Archived channels don't even reach `failed`
            # any more (see slack.py) -- what's left here is worth seeing,
            # a few at a time, not all at once.
            detail = "; ".join(failed[:5]) + (f"; +{len(failed) - 5} more" if len(failed) > 5 else "")
            notes.append(
                f"Slack auto-join: {len(join_result['joined'])} newly joined, "
                f"{len(join_result['already_in'])} already in, "
                f"{len(join_result['skipped_archived'])} archived (skipped), "
                f"{len(failed)} failed"
                + (f" ({detail})" if failed else "")
            )
    except Exception as exc:
        notes.append(f"Slack auto-join-all-channels failed: {exc}")

    # --- Full-context gather + real Google Ads spend, per Atlas account
    # (2026-08-06). Deliberately unbounded/inefficient (see
    # account_context_gather.py). Spend here is informational/cross-check
    # data only now -- it does not gate live_accounts (see above). ---
    rich_context: dict[str, list[str]] = {}
    context_gather_diagnostics: dict[str, dict] = {}
    google_ads_client = GoogleAdsClient()
    meta_ads_client = MetaAdsClient()
    live_google_ads_spend: dict[str, dict] = {}
    live_meta_ads_spend: dict[str, dict] = {}
    # Only ever populated when the account HAS a real ID on file but the pull
    # itself failed -- a genuine problem worth a flagged line on the
    # dashboard, distinct from "no ID at all" (2026-08-10: Bob's operating
    # assumption that CID/ACT-ID absence confirms the account simply doesn't
    # have that service, not an open question anymore -- see
    # dashboard_summary.py's ad_platform_errors param).
    ad_platform_errors: dict[str, dict] = {}
    try:
        for account_name in all_matched_accounts(account_context):
            ctx = account_context.get(account_name, {})
            needs_monitoring = _needs_active_monitoring(ctx.get("go_live_deadline"), account_name in live_accounts)
            diagnostics = {
                "full_gather_skipped": not needs_monitoring,
                "clickup_ok": None, "clickup_comment_count": None, "clickup_error": None,
                "slack_channel_matched": None, "slack_match_confidence": None, "slack_match_score": None,
                "slack_ok": None, "slack_message_count": None, "slack_error": None,
            }
            if needs_monitoring:
                gather_result = gather_atlas_context(
                    ctx.get("clickup_folder_id"),
                    ctx.get("slack_channel_id"),
                    clickup,
                    slack,
                )
                rich_context[account_name] = gather_result.context
                diagnostics.update({
                    "clickup_ok": gather_result.clickup_ok,
                    "clickup_comment_count": gather_result.clickup_comment_count,
                    "clickup_error": gather_result.clickup_error,
                    "slack_channel_matched": gather_result.slack_channel_matched,
                    "slack_match_confidence": gather_result.slack_match_confidence,
                    "slack_match_score": gather_result.slack_match_score,
                    "slack_ok": gather_result.slack_ok,
                    "slack_message_count": gather_result.slack_message_count,
                    "slack_error": gather_result.slack_error,
                })

            # Real Google Ads spend via adspend/ — the SOLE spend/is_live
            # source now (2026-08-06, heartbeat dropped entirely). Soft-failed
            # like every other per-account external call here: a bad/missing
            # customer_id shouldn't drop the account's other context or the run.
            customer_id = ctx.get("google_ads_customer_id")
            diagnostics["google_ads_live_ok"] = None
            diagnostics["google_ads_live_error"] = None
            if customer_id:
                try:
                    spend = google_ads_client.get_account_spend(customer_id, date_range="LAST_7_DAYS")
                    live_google_ads_spend[account_name] = {
                        "spend": spend["total_cost"],
                        "enabled_campaigns": spend["enabled_campaign_count"],
                        "impressions": spend["total_impressions"],
                        "clicks": spend["total_clicks"],
                        "conversions": spend["total_conversions"],
                        # Live campaigns + anything with activity in the window (see
                        # filter_relevant_campaigns) -- not the full list, which
                        # includes every REMOVED campaign the account has ever had.
                        "campaigns": filter_relevant_campaigns(spend["campaigns"]),
                    }
                    diagnostics["google_ads_live_ok"] = True
                except Exception as exc:
                    diagnostics["google_ads_live_ok"] = False
                    diagnostics["google_ads_live_error"] = str(exc)
                    ad_platform_errors.setdefault(account_name, {})["Google Ads"] = str(exc)

            # Real Meta spend (2026-08-06) — same shape/soft-fail contract as
            # Google above. Atlas's metaAdAccountId comes pre-formatted with
            # the "act_" prefix already, no parsing needed.
            meta_ad_account_id = ctx.get("meta_ad_account_id")
            diagnostics["meta_ads_live_ok"] = None
            diagnostics["meta_ads_live_error"] = None
            if meta_ad_account_id:
                try:
                    meta_spend = meta_ads_client.get_account_spend(meta_ad_account_id, date_range="LAST_7_DAYS")
                    live_meta_ads_spend[account_name] = {
                        "spend": meta_spend["total_cost"],
                        "enabled_campaigns": meta_spend["enabled_campaign_count"],
                        "impressions": meta_spend["total_impressions"],
                        "clicks": meta_spend["total_clicks"],
                        "conversions": meta_spend["total_conversions"],
                        "campaigns": filter_relevant_campaigns(meta_spend["campaigns"]),
                    }
                    diagnostics["meta_ads_live_ok"] = True
                except Exception as exc:
                    diagnostics["meta_ads_live_ok"] = False
                    diagnostics["meta_ads_live_error"] = str(exc)
                    ad_platform_errors.setdefault(account_name, {})["Meta"] = str(exc)

            context_gather_diagnostics[account_name] = diagnostics
    except Exception as exc:
        notes.append(f"Rich context gather failed, narratives will use flag messages only: {exc}")

    run.context_gather_json = json.dumps(context_gather_diagnostics)

    # --- Go-live target status per account (2026-08-06) — see
    # _go_live_target_status: behind/approaching/on_track against Atlas's own
    # deadlines.goLive, "live" once Atlas's own stage says so (see
    # live_accounts above). Stored on account_context alongside day/stage so
    # dashboard_summary.py can read it the same way. ---
    for account_name, ctx in account_context.items():
        ctx["target_status"] = _go_live_target_status(ctx.get("go_live_deadline"), account_name in live_accounts)

    # --- "Ads off — who's dark and why" classification, per the reference
    # dashboard (golive-pipeline-dashboard.pdf) — cross-platform (Google Ads +
    # Meta) and Atlas-stage-driven throughout: "should be on but dark" reads
    # Atlas's stage directly and only fires if EVERY platform this account
    # has data for is dark, so a client live via Meta never gets flagged for
    # a $0 Google campaign; "verified off" trusts Atlas's stage=="closed"
    # outright ("assume Atlas will always have perfect data regarding
    # account status" — Bob, 2026-08-06), no independent ad-platform
    # re-verification. billing_unsettled is a placeholder — see
    # ads_off_classification.py. An account with no googleMccId/
    # metaAdAccountId or a failed pull is skipped for that platform — we
    # simply don't have data to classify it, not confirmed dark. ---
    for account_name in sorted(all_account_names):
        classification = classify_ads_off(
            stage=account_context.get(account_name, {}).get("stage"),
            google_ads=live_google_ads_spend.get(account_name),
            meta_ads=live_meta_ads_spend.get(account_name),
        )

        if classification.should_be_on_but_dark:
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.ads_off_should_be_on,
                    severity=FlagSeverity.urgent,
                    client_name=account_name,
                    message="Atlas stage says live/optimizations but every mapped ad platform is dark — verify account mapping or billing",
                    created_at=datetime.utcnow(),
                )
            )
        for platform in classification.campaigns_on_zero_spend:
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.ads_off_zero_spend,
                    severity=FlagSeverity.warning,
                    client_name=account_name,
                    message=f"{platform}: campaigns enabled, $0 spend — billing/payment check",
                    created_at=datetime.utcnow(),
                )
            )
        if classification.billing_unsettled:
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.ads_off_unsettled,
                    severity=FlagSeverity.urgent,
                    client_name=account_name,
                    message="Payment unsettled — ad delivery blocked",
                    created_at=datetime.utcnow(),
                )
            )
        if classification.verified_off:
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.ads_off_verified_off,
                    severity=FlagSeverity.info,
                    client_name=account_name,
                    message="Atlas shows this account closed",
                    created_at=datetime.utcnow(),
                )
            )

    # --- Ad spend summary per account, for the accounts overview + its LLM
    # narrative — real Google Ads + Meta spend (heartbeat dropped entirely). ---
    spend_by_account: dict[str, dict] = {}
    for account_name, live_spend in live_google_ads_spend.items():
        spend_by_account.setdefault(account_name, {})["Google Ads"] = live_spend
    for account_name, live_spend in live_meta_ads_spend.items():
        spend_by_account.setdefault(account_name, {})["Meta"] = live_spend

    # --- Accounts overview/chart only cover the active-monitoring subset
    # (see _needs_active_monitoring) -- an established, long-past-due-live
    # account is dropped from these (and from the LLM narrative call) but
    # still fully covered by the ads-off classification above, which reads
    # live_google_ads_spend/live_meta_ads_spend directly, not this dict. ---
    active_account_context = {
        name: ctx for name, ctx in account_context.items()
        if _needs_active_monitoring(ctx.get("go_live_deadline"), name in live_accounts)
    }

    db.add_all(flags)
    digest_text = build_digest(flags)
    run.digest_text = digest_text
    try:
        run.dashboard_json = build_dashboard_json(
            flags,
            active_account_context,
            all_account_names,
            live_accounts,
            previous_live_accounts,
            rich_context,
            spend_by_account,
            web_builds,
            ad_platform_errors,
        )
        narrative_error = json.loads(run.dashboard_json).get("narrative_error")
        if narrative_error:
            notes.append(f"Dashboard narrative synthesis failed, showing raw flags instead: {narrative_error}")
    except Exception as exc:
        notes.append(f"Dashboard JSON build failed entirely: {exc}")
    run.status = RunStatus.success if not notes else RunStatus.partial
    run.notes = "\n".join(notes) if notes else None
    run.finished_at = datetime.utcnow()
    db.commit()

    slack.send_dm(settings.slack_christian_user_id, digest_text)

    return run
