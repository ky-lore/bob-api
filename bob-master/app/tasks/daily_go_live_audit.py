"""
Port of tasks/daily-go-live-audit/SKILL.md.

FULLY MACRO as of 2026-07-31 (Bob's explicit call): the original package-type
clock-threshold branching (marketing 14d/21d, website 10d, custom-ETA,
SEO-same-week, etc. — SKILL.md's PACKAGE CLOCKS section) has been ripped out
entirely. No more per-package day thresholds, no more "exempt" packages, no
more package identification gating anything. Every account gets treated the
same way: day count since signing, live/not-live (from heartbeat spend), ad
spend summary, full ClickUp+Slack context, and one LLM-synthesized "where do
they stand" sentence — see dashboard_summary.py and account_context_gather.py.

ATLAS-DRIVEN as of 2026-08-04 (Bob's explicit call, after Atlas — his
proprietary internal API — went live): Atlas is now the authoritative account
universe and the source of stage + day-count, replacing ClickUp-card fuzzy
matching entirely. `createdAt` is the true origin date (not a Slack/ClickUp
proxy). Full-context gather uses Atlas's exact clickupFolderId /
internalSlackChannelId directly — no channel-name or card-title fuzzy
matching anywhere in that path anymore (see account_context_gather.py's
gather_atlas_context). The ONE fuzzy match still standing: heartbeat sheet
account names (Google Ads/Meta don't carry Atlas IDs) against Atlas's clean
companyName — much higher-fidelity target than a ClickUp card title ever
was, and a miss here is now flagged (action_needed), closing a gap this
module used to carry as a silent-skip TODO.

What's fully implemented below (deterministic, spec'd precisely in the prompt,
or built against real sandboxed ClickUp/Atlas data — see chat history):
  - heartbeat sheet pull, freshness check, CSV-export fallback
  - the LIVE DEFINITION cross-check, now genuinely cross-platform (a client
    live via Meta no longer gets flagged for a $0 legacy campaign on Google)
  - Atlas account correlation (stage, day-count, exact Slack/ClickUp IDs) —
    see app/integrations/atlas_client.py
  - ex-client filtering against the admin-editable list (independent of, and
    layered alongside, Atlas's own isActive flag — kept both on purpose,
    conservative call, since removing either wasn't confidently justified yet)
  - digest assembly from persisted flags, and AuditRun/Flag persistence
  - full-context gather for EVERY Atlas account (full ClickUp folder — every
    List's every Task's comments — + full Slack channel history) via exact
    Atlas IDs — see app/tasks/account_context_gather.py's gather_atlas_context
  - auto-joins every public Slack channel the bot isn't already in before
    gathering — channel_history() silently fails on a channel the bot hasn't
    joined, exact ID or not. Private channels still need a manual bot invite;
    there's no Slack API for a bot to self-join one.
  - real Google Ads spend (2026-08-06) via adspend/ (see adspend/README.md) —
    a self-contained package mounted at /adspend on this same app (app/main.py)
    rather than deployed as a separate service, and called in-process here
    (no self-HTTP hop — it's the same process) — additive alongside the
    heartbeat number, not a replacement, for any account with a googleMccId
    on file. Meta not wired in yet.

What's intentionally left as TODOs — these require judgment calls this port
should not guess at (see docs/TASK-INVENTORY.md and the chat history this was
built from for why):
  - the GHL Closed-Won -> new ClickUp card flow, including reading sales notes
    for the "over-promise" check (Atlas's own salesNotes field may make this
    moot — not yet wired into the narrative context)
  - stage-aware checks that require correlating ClickUp card state with Slack
    channel activity (needs the org-wide search decision — see integrations/slack.py)
  - evidence-conflict handling across board/Slack/heartbeat/GHL (ACCURACY RULES §5)
  - retention pipeline / ex-client mapping hasn't been reconciled against
    Atlas's own stage="closed" and isActive fields — both systems are kept
    running independently for now rather than assuming overlap
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from adspend.google_ads_client import GoogleAdsClient
from app.config import get_settings
from app.integrations.atlas_client import AtlasClient
from app.integrations.clickup import ClickUpClient
from app.integrations.ghl import GHLClient
from app.integrations.google_drive import GoogleDriveClient
from app.integrations.slack import SlackClient
from app.models import AuditRun, Flag, FlagCategory, FlagSeverity, ManagedClientEntry, ManagedListType, RunStatus
from app.tasks.account_context_gather import gather_atlas_context
from app.tasks.ads_off_classification import classify_ads_off
from app.tasks.clickup_correlation import resolve_day_count
from app.tasks.dashboard_summary import all_matched_accounts, build_dashboard_json
from app.tasks.matching import find_best_match, identity_name
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


def _days_since_atlas_created_at(created_at: str | None) -> int:
    """Atlas's createdAt is the true origin date (Bob, 2026-08-04) — replaces
    the old ClickUp date_created / manual "Day N" title-marker heuristic
    entirely. Manual .replace("Z", "+00:00") rather than relying on
    fromisoformat's native "Z" handling, which only exists from Python 3.11."""
    if not created_at:
        return 0
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    now = datetime.now(created.tzinfo) if created.tzinfo else datetime.utcnow()
    return (now - created).days


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

    # --- Atlas account correlation (Bob, 2026-08-04): Atlas is now the
    # authoritative account universe and the source of stage + day-count,
    # replacing ClickUp-card fuzzy matching entirely. Heartbeat rows don't
    # carry an Atlas ID, so one fuzzy match still happens here — but against
    # Atlas's clean companyName, not a messy ClickUp card title, which is why
    # identity_name (no stripping at all) is the right extractor. A wrong or
    # missing match here now gets flagged instead of silently dropped,
    # closing the "no matched card" gap this module used to carry as a TODO. ---
    clickup = ClickUpClient()
    try:
        atlas_accounts = [a for a in AtlasClient().get_all_accounts() if a.get("isActive")]
    except Exception as exc:
        atlas_accounts = []
        notes.append(f"Atlas accounts pull failed: {exc}")

    # DEBUG cap (Bob, 2026-08-06, temporary — see Settings.debug_max_accounts):
    # sorted by companyName first so the same accounts show up every run
    # while debugging, rather than whatever order Atlas happens to return.
    if settings.debug_max_accounts is not None and len(atlas_accounts) > settings.debug_max_accounts:
        atlas_accounts = sorted(atlas_accounts, key=lambda a: a.get("companyName") or "")[: settings.debug_max_accounts]
        notes.append(
            f"DEBUG: capped to {settings.debug_max_accounts} of the full Atlas account list "
            "(Settings.debug_max_accounts) — not a real run, remove the cap when done debugging"
        )

    alias_map = get_alias_map(db)
    atlas_targets = [
        {"id": a["id"], "name": a["companyName"]} for a in atlas_accounts if a.get("id") and a.get("companyName")
    ]
    heartbeat_name_to_atlas_id: dict[str, str] = {}

    for heartbeat_name in sorted(all_account_names):
        match = find_best_match(heartbeat_name, atlas_targets, aliases=alias_map, name_extractor=identity_name)

        if match.confidence in ("exact", "alias", "high"):
            heartbeat_name_to_atlas_id[heartbeat_name] = match.card_id
        elif match.confidence == "ambiguous":
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.action_needed,
                    severity=FlagSeverity.warning,
                    client_name=heartbeat_name,
                    message=(
                        f'Ambiguous Atlas match — closest client is "{match.card_name}" '
                        f"(similarity {match.score:.2f}); confirm or fix the spelling in Atlas"
                    ),
                    unverified=True,
                    created_at=datetime.utcnow(),
                )
            )
        else:
            flags.append(
                Flag(
                    run_id=run.id,
                    category=FlagCategory.action_needed,
                    severity=FlagSeverity.info,
                    client_name=heartbeat_name,
                    message="Heartbeat spend data doesn't match any active Atlas client — needs manual linking",
                    unverified=True,
                    created_at=datetime.utcnow(),
                )
            )

    atlas_id_to_heartbeat_name = {v: k for k, v in heartbeat_name_to_atlas_id.items()}
    heartbeat_live_accounts, heartbeat_rows_by_account = live_accounts, rows_by_account

    # From here on, all_account_names / account_context / live_accounts /
    # rows_by_account are Atlas-companyName-keyed — every section below this
    # point (retention check, ads-off classification, rich-context gather,
    # dashboard build) reads these same names unchanged.
    all_account_names = set()
    account_context: dict[str, dict] = {}  # companyName -> {day, stage, atlas_id, clickup_folder_id, slack_channel_id}
    live_accounts = set()
    rows_by_account = {}

    for atlas_account in atlas_accounts:
        company_name = atlas_account.get("companyName")
        if not company_name:
            continue
        all_account_names.add(company_name)
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
        }

        heartbeat_name = atlas_id_to_heartbeat_name.get(atlas_account.get("id"))
        if heartbeat_name:
            if heartbeat_name in heartbeat_live_accounts:
                live_accounts.add(company_name)
            if heartbeat_name in heartbeat_rows_by_account:
                rows_by_account[company_name] = heartbeat_rows_by_account[heartbeat_name]

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

    # --- Full-context gather for narrative synthesis (Bob, 2026-08-04): Atlas's
    # exact clickupFolderId / internalSlackChannelId now, no fuzzy matching or
    # channel-list fetch at all — every matched account gets the same
    # treatment, full ClickUp folder (lists -> tasks -> comments) + full Slack
    # channel history, no truncation. Deliberately unbounded/inefficient. ---
    rich_context: dict[str, list[str]] = {}
    context_gather_diagnostics: dict[str, dict] = {}
    google_ads_client = GoogleAdsClient()
    live_google_ads_spend: dict[str, dict] = {}
    try:
        for account_name in all_matched_accounts(account_context):
            ctx = account_context.get(account_name, {})
            gather_result = gather_atlas_context(
                ctx.get("clickup_folder_id"),
                ctx.get("slack_channel_id"),
                clickup,
                slack,
            )
            rich_context[account_name] = gather_result.context
            diagnostics = {
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

            # Real Google Ads spend via adspend/ (2026-08-06), additive
            # alongside the heartbeat-sourced number below rather than
            # replacing it — surfacing both side by side is deliberate, it's
            # exactly what exposes heartbeat-sheet drift/staleness. Soft-failed
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
                    }
                    diagnostics["google_ads_live_ok"] = True
                except Exception as exc:
                    diagnostics["google_ads_live_ok"] = False
                    diagnostics["google_ads_live_error"] = str(exc)

            context_gather_diagnostics[account_name] = diagnostics
    except Exception as exc:
        notes.append(f"Rich context gather failed, narratives will use flag messages only: {exc}")

    run.context_gather_json = json.dumps(context_gather_diagnostics)

    # --- Ad spend summary per account (Google Ads / Meta), for the accounts
    # overview + its LLM narrative — heartbeat rows from Step 1 (no package
    # logic involved), plus real Google Ads spend via adspend where a
    # googleMccId/customer ID is on file (see live_google_ads_spend above). ---
    spend_by_account: dict[str, dict] = {
        account_name: {
            label: {"spend": row.total_spend, "enabled_campaigns": row.enabled_campaigns}
            for label, row in platform_rows.items()
        }
        for account_name, platform_rows in rows_by_account.items()
    }
    for account_name, live_spend in live_google_ads_spend.items():
        spend_by_account.setdefault(account_name, {})["Google Ads (live)"] = live_spend

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
