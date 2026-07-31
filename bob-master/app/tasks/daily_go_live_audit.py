"""
Port of tasks/daily-go-live-audit/SKILL.md.

FULLY MACRO as of 2026-07-31 (Bob's explicit call): the original package-type
clock-threshold branching (marketing 14d/21d, website 10d, custom-ETA,
SEO-same-week, etc. — SKILL.md's PACKAGE CLOCKS section) has been ripped out
entirely. No more per-package day thresholds, no more "exempt" packages, no
more package identification gating anything. Every account matched to a
go-live-list card now gets treated the same way: day count since signing,
live/not-live (from heartbeat spend), ad spend summary, full ClickUp+Slack
context, and one LLM-synthesized "where do they stand" sentence — see
dashboard_summary.py and account_context_gather.py. The idea is to see the
whole board's real state before re-introducing any granular rules.

What's fully implemented below (deterministic, spec'd precisely in the prompt,
or built against real sandboxed ClickUp data — see chat history):
  - heartbeat sheet pull, freshness check, CSV-export fallback
  - the LIVE DEFINITION cross-check, now genuinely cross-platform (a client
    live via Meta no longer gets flagged for a $0 legacy campaign on Google)
  - ClickUp board correlation via app/tasks/matching.py (fuzzy name matching)
  - ex-client filtering against the admin-editable list (replaces the hardcoded list)
  - digest assembly from persisted flags, and AuditRun/Flag persistence
  - full-context gather for EVERY matched account (full ClickUp card+subtask
    comments, full "internal-<client>" Slack channel history) via the same
    fuzzy account matching used elsewhere — see
    app/tasks/account_context_gather.py. Deliberately a stopgap: Bob is
    building "Atlas", a proprietary source-of-truth API for exact per-account
    IDs (ClickUp, Slack, ad platforms), which will replace the fuzzy matching
    here once it exists (2026-07-31 decision)
  - auto-joins every public Slack channel the bot isn't already in before
    gathering (2026-07-31) — channel_history() silently fails on a channel
    the bot hasn't joined regardless of how good the name match is. Liberal
    on purpose: Slack context matching now accepts "ambiguous" confidence
    too, not just exact/alias/high (see account_context_gather.py) — a wrong
    channel match just means extra context, not a wrong account correlation,
    so the same caution as ClickUp/retention board matching isn't warranted.
    Private channels still need a manual bot invite; there's no Slack API for
    a bot to self-join one.

What's intentionally left as TODOs — these require judgment calls this port
should not guess at (see docs/TASK-INVENTORY.md and the chat history this was
built from for why):
  - accounts with heartbeat/GHL data but NO matched ClickUp card are currently
    silently skipped, not flagged — could mean a real "no card exists" gap or
    just a matching miss; needs real volume data before deciding which
  - the GHL Closed-Won -> new ClickUp card flow, including reading sales notes
    for the "over-promise" check
  - stage-aware checks that require correlating ClickUp card state with Slack
    channel activity (needs the org-wide search decision — see integrations/slack.py)
  - evidence-conflict handling across board/Slack/heartbeat/GHL (ACCURACY RULES §5)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.clickup import ClickUpClient
from app.integrations.ghl import GHLClient
from app.integrations.google_drive import GoogleDriveClient
from app.integrations.slack import SlackClient
from app.models import AuditRun, Flag, FlagCategory, FlagSeverity, ManagedClientEntry, ManagedListType, RunStatus
from app.tasks.account_context_gather import gather_rich_context
from app.tasks.ads_off_classification import classify_ads_off
from app.tasks.clickup_correlation import resolve_day_count
from app.tasks.dashboard_summary import all_matched_accounts, build_dashboard_json
from app.tasks.matching import find_best_match
from app.tasks.retention_check import (
    ACTIVE_RISK_STATUSES,
    CHURNED_STATUSES,
    extract_retention_candidate_name,
    is_administrative_card,
)

# Real examples from the Meta sheet: "106231623122110", "129853217452321" —
# accounts never assigned a name in Meta Business Manager, per
# GoLive_Audit_Dev_Handover_Brief.md §1.
_NUMERIC_ONLY_NAME_RE = re.compile(r"^\d+$")


@dataclass
class HeartbeatRow:
    account_name: str
    enabled_campaigns: int
    am_build_spend: float
    legacy_spend: float
    lsa_spend: float
    checked_at: datetime
    # Meta-only ("Account status": ACTIVE / UNSETTLED / DISABLED, per
    # GoLive_Audit_Dev_Handover_Brief.md §1) — empty string on the Google Ads
    # sheet, which has no equivalent column.
    account_status: str = ""

    @property
    def total_spend(self) -> float:
        return self.am_build_spend + self.legacy_spend + self.lsa_spend


def parse_heartbeat_rows(raw_rows: list[list[str]]) -> list[HeartbeatRow]:
    """Assumes row 0 is a header row; matches columns by name substring rather
    than position, since exact column order isn't documented anywhere in this
    package — confirm header names against the real sheet before first run."""
    if not raw_rows:
        return []
    header = [h.strip().lower() for h in raw_rows[0]]

    def col(*substrings: str) -> int | None:
        for i, h in enumerate(header):
            if any(s in h for s in substrings):
                return i
        return None

    def col_all(*substrings: str) -> int | None:
        for i, h in enumerate(header):
            if all(s in h for s in substrings):
                return i
        return None

    name_i = col("account", "client")
    enabled_i = col("enabled campaign")
    am_build_i = col("am-build", "am build")
    legacy_i = col("legacy")
    # Bare "lsa" would match "Enabled LSA" (a yes/no flag) ahead of the real
    # dollar columns "Spend yesterday/today (LSA)" — confirmed against the real
    # sheet header via GET /admin/heartbeat/headers. That bug made lsa_spend
    # read as 0 for every account, misclassifying LSA-only-live clients as
    # legacy-only on the first real run.
    lsa_i = col_all("lsa", "yesterday") or col_all("lsa", "today") or col("lsa")
    checked_i = col("checked at")
    # Meta-only. col_all (not col) — bare "account" would hit "Account name" first.
    account_status_i = col_all("account", "status")

    rows = []
    for raw in raw_rows[1:]:
        if not raw or name_i is None:
            continue

        account_name = raw[name_i].strip() if name_i < len(raw) else ""
        if not account_name:
            # Blank/subtotal/spacer rows in the real sheet — not a real account.
            continue

        def get_float(i: int | None) -> float:
            if i is None or i >= len(raw) or not raw[i]:
                return 0.0
            try:
                return float(raw[i].replace("$", "").replace(",", ""))
            except ValueError:
                return 0.0

        rows.append(
            HeartbeatRow(
                account_name=account_name,
                enabled_campaigns=int(get_float(enabled_i)),
                am_build_spend=get_float(am_build_i),
                legacy_spend=get_float(legacy_i),
                lsa_spend=get_float(lsa_i),
                checked_at=_parse_checked_at(raw[checked_i]) if checked_i is not None and checked_i < len(raw) else datetime.min,
                account_status=(
                    raw[account_status_i].strip().upper()
                    if account_status_i is not None and account_status_i < len(raw)
                    else ""
                ),
            )
        )
    return rows


def _parse_checked_at(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return datetime.min


def is_live(row: HeartbeatRow) -> bool:
    """LIVE DEFINITION (critical, per SKILL.md): AM-BUILD or LSA spend counts as
    live. Legacy-only spend does NOT count as live — flag it separately instead."""
    return row.am_build_spend > 0 or row.lsa_spend > 0


def legacy_only_spend_flag(row: HeartbeatRow) -> bool:
    return row.legacy_spend > 0 and row.am_build_spend == 0 and row.lsa_spend == 0


def get_active_client_names(db: Session, list_type: ManagedListType) -> set[str]:
    rows = db.query(ManagedClientEntry).filter_by(list_type=list_type, active=True).all()
    return {r.client_name for r in rows}


def _account_dedupe_key(name: str) -> str:
    """Casefold + collapse whitespace only — light-touch dedup for the same
    client appearing with slightly different spelling across the Google Ads
    vs Meta heartbeat sheets (real example: "vera plumbing and drain" on one
    sheet, "Vera Plumbing and Drain" on the other, processed as two separate
    accounts and double-flagged). Deliberately lighter than matching.normalize()
    — that also strips legal suffixes/punctuation for cross-system fuzzy
    matching, which would risk over-merging genuinely different accounts here."""
    return re.sub(r"\s+", " ", name.strip().casefold())


def get_alias_map(db: Session) -> dict[str, str]:
    """Normalized alias -> canonical map for matching.find_best_match, sourced
    from the human-maintained alias table (/admin/watchlist, list_type=alias).
    client_name = canonical/heartbeat-side name, note = the ClickUp-side name."""
    from app.tasks.matching import normalize

    rows = db.query(ManagedClientEntry).filter_by(list_type=ManagedListType.alias, active=True).all()
    return {normalize(r.note): normalize(r.client_name) for r in rows if r.note}


def build_digest(flags: list[Flag]) -> str:
    """Assembles the six-section digest per SKILL.md DO #6. Caps section length
    loosely toward the ~40-line target — trim further once real flag volume is known."""
    if not flags:
        return "Go-live audit: all clear today. :white_check_mark:"

    sections = [
        (":rotating_light: Action needed today", FlagCategory.action_needed),
        (":bar_chart: Heartbeat mismatches", FlagCategory.heartbeat_mismatch),
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

    ex_clients = get_active_client_names(db, ManagedListType.ex_client)
    drive = GoogleDriveClient()
    ghl = GHLClient()
    flags: list[Flag] = []
    notes: list[str] = []

    # --- Step 1: heartbeat sheets, freshness + CSV fallback ---
    # Collected across both sheets before any legacy-only flagging, so the
    # LIVE DEFINITION cross-check is genuinely cross-platform (see module
    # docstring) rather than judging each platform in isolation.
    live_accounts: set[str] = set()
    all_account_names: set[str] = set()
    canonical_names: dict[str, str] = {}  # dedupe key -> display name (first-seen spelling wins)
    heartbeat_rows: list[tuple[str, HeartbeatRow, str]] = []  # (platform label, row, canonical account name)

    for label, file_id, tab in (
        ("Google Ads", settings.drive_google_ads_heartbeat_file_id, settings.drive_google_ads_heartbeat_tab),
        ("Meta", settings.drive_meta_heartbeat_file_id, settings.drive_meta_heartbeat_tab),
    ):
        try:
            raw_rows = drive.read_sheet_values(file_id, tab)
        except Exception as exc:
            notes.append(f"{label} heartbeat sheet unreadable by both methods: {exc}")
            continue

        rows = [r for r in parse_heartbeat_rows(raw_rows) if r.account_name not in ex_clients]
        for row in rows:
            if _NUMERIC_ONLY_NAME_RE.match(row.account_name):
                # Per GoLive_Audit_Dev_Handover_Brief.md §1: an account whose
                # name is just a numeric ID (real examples: "106231623122110")
                # was never assigned a name in Meta Business Manager — it
                # can't be matched to any client by name. Flag it as needing
                # manual resolution instead of silently attempting (and
                # failing) fuzzy matching against it.
                flags.append(
                    Flag(
                        run_id=run.id,
                        category=FlagCategory.action_needed,
                        severity=FlagSeverity.info,
                        client_name=row.account_name,
                        message=f"{label}: unmapped account (numeric ID only, no name) — needs name resolution",
                        unverified=True,
                        created_at=datetime.utcnow(),
                    )
                )
                continue

            canonical_name = canonical_names.setdefault(_account_dedupe_key(row.account_name), row.account_name)
            all_account_names.add(canonical_name)
            if is_live(row):
                live_accounts.add(canonical_name)
            heartbeat_rows.append((label, row, canonical_name))
            if row.checked_at != datetime.min and GoogleDriveClient.is_stale(row.checked_at):
                notes.append(f"{label} heartbeat sheet stale for {row.account_name} (checked_at {row.checked_at})")

    rows_by_account: dict[str, dict[str, HeartbeatRow]] = {}
    for label, row, canonical_name in heartbeat_rows:
        rows_by_account.setdefault(canonical_name, {})[label] = row

    for label, row, canonical_name in heartbeat_rows:
        if legacy_only_spend_flag(row) and canonical_name not in live_accounts:
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.heartbeat_mismatch,
                    severity=FlagSeverity.warning,
                    client_name=canonical_name,
                    message=f"{label}: legacy campaign burning client budget — confirm intent",
                    created_at=datetime.utcnow(),
                )
            )

    # --- ClickUp board correlation (fully macro — see module docstring: no
    # package identification, no clock-threshold evaluation, no flags here
    # anymore. Every matched account just gets recorded for the dashboard;
    # "where they stand" is now the LLM narrative's job, not this loop's) ---
    try:
        clickup = ClickUpClient()
        board_data = clickup.get_list_tasks(settings.clickup_go_live_list_id, include_closed=True)
        cards = [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "status": ((t.get("status") or {}).get("status") or "").lower(),
                "tags": [tag.get("name") for tag in t.get("tags", [])],
                "date_created": t.get("date_created"),
            }
            for t in board_data.get("tasks", [])
        ]
    except Exception as exc:
        cards = []
        notes.append(f"ClickUp Go-Live board pull failed: {exc}")

    alias_map = get_alias_map(db)
    account_context: dict[str, dict] = {}  # account_name -> {"day": int, "stage": str, "card_id": str}

    for account_name in sorted(all_account_names):
        match = find_best_match(account_name, cards, aliases=alias_map)

        if match.confidence == "ambiguous":
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.action_needed,
                    severity=FlagSeverity.warning,
                    client_name=account_name,
                    message=(
                        f'Ambiguous ClickUp match — closest card is "{match.card_name}" '
                        f"(similarity {match.score:.2f}); confirm or add an alias at /admin/watchlist"
                    ),
                    unverified=True,
                    created_at=datetime.utcnow(),
                )
            )
            continue

        if match.confidence == "none":
            # No card found at all — could be a real gap or a matching miss.
            # Not flagged yet; see module docstring TODO.
            continue

        card = next((c for c in cards if c["id"] == match.card_id), None)
        if not card:
            continue

        days_elapsed = resolve_day_count(card["name"], card["date_created"]) if card["date_created"] else 0
        # Recorded for every matched card, including ignore/complete — the
        # dashboard shows every matched account regardless of status now.
        account_context[account_name] = {
            "day": days_elapsed,
            "stage": card["status"],
            "card_id": card["id"],
        }

    # --- Retention pipeline cancel-intent cross-check (ACCURACY RULES §2:
    # "board LIVE is not proof of active... check the Retention pipeline
    # before reporting anyone live"). Administrative/template/demo cards
    # (confirmed present on the real board — see retention_check.py) are
    # filtered before matching is even attempted. ---
    try:
        retention_clickup = ClickUpClient()
        retention_data = retention_clickup.get_list_tasks(settings.clickup_retention_list_id, include_closed=True)
        retention_cards = [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "status": ((t.get("status") or {}).get("status") or "").lower(),
            }
            for t in retention_data.get("tasks", [])
            if not is_administrative_card(t.get("name", ""))
        ]
    except Exception as exc:
        retention_cards = []
        notes.append(f"Retention pipeline pull failed: {exc}")

    retention_status_by_account: dict[str, str] = {}

    for account_name in sorted(all_account_names):
        retention_match = find_best_match(
            account_name, retention_cards, name_extractor=extract_retention_candidate_name
        )
        if retention_match.confidence not in ("exact", "alias", "high"):
            continue

        retention_card = next((c for c in retention_cards if c["id"] == retention_match.card_id), None)
        if not retention_card:
            continue

        status = retention_card["status"]
        retention_status_by_account[account_name] = status
        if status in CHURNED_STATUSES:
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.action_needed,
                    severity=FlagSeverity.urgent,
                    client_name=account_name,
                    message=(
                        f'Retention pipeline shows CHURNED ("{retention_card["name"]}") — '
                        "do not report as live/went-live regardless of heartbeat data, per ACCURACY RULES §2"
                    ),
                    evidence_url=f"https://app.clickup.com/t/{retention_card['id']}",
                    unverified=True,
                    created_at=datetime.utcnow(),
                )
            )
        elif status in ACTIVE_RISK_STATUSES:
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.action_needed,
                    severity=FlagSeverity.warning,
                    client_name=account_name,
                    message=(
                        f'Active cancel/save request in Retention pipeline (status: "{status}", '
                        f'card: "{retention_card["name"]}") — confirm before reporting as live'
                    ),
                    evidence_url=f"https://app.clickup.com/t/{retention_card['id']}",
                    unverified=True,
                    created_at=datetime.utcnow(),
                )
            )

    # --- Web Build Pipeline sweep (Bob, 2026-07-31): a separate ClickUp list
    # tracking website builds, not client accounts -- outside the 90-day
    # go-live board window entirely (real example the original dashboard
    # named: ColdRiite Walk-Ins sitting 374 days). Still macro, no >30-day
    # threshold/flag rule yet -- just card name/status/day count, oldest
    # first, same "see the whole board before adding rules" approach as the
    # rest of this pass. No heartbeat/live-status join needed here since
    # these aren't ad accounts. ---
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

    # --- "Ads off — who's dark and why" classification, per the reference
    # dashboard (golive-pipeline-dashboard.pdf). Iterates heartbeat-known
    # accounts — does NOT catch an account fully unmapped on BOTH platforms
    # that still has a "live" card (the reference dashboard's own "Shelby
    # Plumbing" example: no Meta heartbeat row at all). Closing that gap needs
    # card-driven discovery in addition to heartbeat-driven, which is a
    # bigger change than this pass — known, accepted limitation for now. ---
    for account_name in sorted(all_account_names):
        platform_rows = rows_by_account.get(account_name, {})
        classification = classify_ads_off(
            google_row=platform_rows.get("Google Ads"),
            meta_row=platform_rows.get("Meta"),
            card_status=account_context.get(account_name, {}).get("stage"),
            retention_status=retention_status_by_account.get(account_name),
            churned_statuses=CHURNED_STATUSES,
        )

        if classification.should_be_on_but_dark:
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.ads_off_should_be_on,
                    severity=FlagSeverity.urgent,
                    client_name=account_name,
                    message="Board says live/optimizations but both platforms are dark — verify account mapping or billing",
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
        if classification.unsettled:
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.ads_off_unsettled,
                    severity=FlagSeverity.urgent,
                    client_name=account_name,
                    message="Meta account status is UNSETTLED — payment is blocking ad delivery",
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
                    message="Retention shows churned/cancelled and heartbeat confirms no active spend — verified off",
                    created_at=datetime.utcnow(),
                )
            )

    # --- Step 3 (partial): GHL Closed Won sweep — data pull wired, card creation is not ---
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

    # --- Full-context gather for narrative synthesis (Bob, 2026-07-31, "go
    # fully macro"): every matched account gets the same treatment now, not
    # just a package-based subset — full ClickUp card+subtask comments, full
    # Slack channel history, no truncation. Built against the existing fuzzy
    # account matching now, ahead of Atlas (unbuilt proprietary source-of-truth
    # API) removing the need for it. Deliberately unbounded/inefficient. ---
    rich_context: dict[str, list[str]] = {}
    context_gather_diagnostics: dict[str, dict] = {}
    try:
        matched_accounts = all_matched_accounts(account_context)
        if matched_accounts:
            slack_channels = slack.list_channels()
            for account_name in matched_accounts:
                gather_result = gather_rich_context(
                    account_name,
                    account_context.get(account_name, {}).get("card_id"),
                    clickup,
                    slack,
                    slack_channels,
                )
                rich_context[account_name] = gather_result.context
                context_gather_diagnostics[account_name] = {
                    "clickup_ok": gather_result.clickup_ok,
                    "clickup_comment_count": gather_result.clickup_comment_count,
                    "clickup_error": gather_result.clickup_error,
                    "slack_channel_matched": gather_result.slack_channel_matched,
                    "slack_match_confidence": gather_result.slack_match_confidence,
                    "slack_match_score": gather_result.slack_match_score,
                    "slack_ok": gather_result.slack_ok,
                    "slack_message_count": gather_result.slack_message_count,
                    "slack_error": gather_result.slack_error,
                }
    except Exception as exc:
        notes.append(f"Rich context gather failed, narratives will use flag messages only: {exc}")

    run.context_gather_json = json.dumps(context_gather_diagnostics)

    # --- Ad spend summary per account (Google Ads / Meta), for the accounts
    # overview + its LLM narrative — straight from the heartbeat rows already
    # pulled in Step 1, no package logic involved. ---
    spend_by_account: dict[str, dict] = {
        account_name: {
            label: {"spend": row.total_spend, "enabled_campaigns": row.enabled_campaigns}
            for label, row in platform_rows.items()
        }
        for account_name, platform_rows in rows_by_account.items()
    }

    db.add_all(flags)
    digest_text = build_digest(flags)
    run.digest_text = digest_text
    try:
        run.dashboard_json = build_dashboard_json(
            flags,
            account_context,
            all_account_names,
            live_accounts,
            previous_live_accounts,
            rich_context,
            spend_by_account,
            web_builds,
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
