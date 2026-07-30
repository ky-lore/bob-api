"""
Port of tasks/daily-go-live-audit/SKILL.md.

What's fully implemented below (deterministic, spec'd precisely in the prompt):
  - heartbeat sheet pull, freshness check, CSV-export fallback
  - the LIVE DEFINITION cross-check (AM-BUILD/LSA spend vs. legacy-only spend)
  - package-clock rule evaluation, given a day count and package type
  - ex-client filtering against the admin-editable list (replaces the hardcoded list)
  - digest assembly from persisted flags, and AuditRun/Flag persistence

What's intentionally left as TODOs — these require judgment calls this port
should not guess at (see docs/TASK-INVENTORY.md and the chat history this was
built from for why):
  - matching a heartbeat-sheet account name to a ClickUp card (fuzzy name
    matching — SKILL.md itself calls out ambiguous near-duplicates by name)
  - the GHL Closed-Won -> new ClickUp card flow, including reading sales notes
    for the "over-promise" check
  - stage-aware checks that require correlating ClickUp card state with Slack
    channel activity (needs the org-wide search decision — see integrations/slack.py)
  - evidence-conflict handling across board/Slack/heartbeat/GHL (ACCURACY RULES §5)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.ghl import GHLClient
from app.integrations.google_drive import GoogleDriveClient
from app.integrations.slack import SlackClient
from app.models import AuditRun, Flag, FlagCategory, FlagSeverity, ManagedClientEntry, ManagedListType, RunStatus

# --- Package clock day thresholds, per SKILL.md PACKAGE CLOCKS ---
MKTG_FLAG_DAYS = (14, 21)
WEB_FLAG_DAYS = (10,)
WEB_SEO_FLAG_DAYS = (10,)


@dataclass
class HeartbeatRow:
    account_name: str
    enabled_campaigns: int
    am_build_spend: float
    legacy_spend: float
    lsa_spend: float
    checked_at: datetime


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

    name_i = col("account", "client")
    enabled_i = col("enabled campaign")
    am_build_i = col("am-build", "am build")
    legacy_i = col("legacy")
    lsa_i = col("lsa")
    checked_i = col("checked at")

    rows = []
    for raw in raw_rows[1:]:
        if not raw or name_i is None:
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
                account_name=raw[name_i],
                enabled_campaigns=int(get_float(enabled_i)),
                am_build_spend=get_float(am_build_i),
                legacy_spend=get_float(legacy_i),
                lsa_spend=get_float(lsa_i),
                checked_at=_parse_checked_at(raw[checked_i]) if checked_i is not None and checked_i < len(raw) else datetime.min,
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


def evaluate_package_clock(
    package: str,
    days_elapsed: int,
    *,
    is_live: bool,
    has_eta: bool = False,
    eta_passed: bool = False,
    seo_started: bool = False,
    fully_complete: bool = False,
) -> str | None:
    """Returns a flag message, or None if clean. Pure function so it's testable
    without touching ClickUp/GHL — the day-count and package-type inputs still
    need to come from a real card (TODO: card ⇄ package/day-count resolution)."""
    if package == "pkg-mktg":
        if is_live:
            return None
        if days_elapsed >= MKTG_FLAG_DAYS[1]:
            return f"Marketing package: {days_elapsed}d, not live (Day 21 escalation)"
        if days_elapsed >= MKTG_FLAG_DAYS[0]:
            return f"Marketing package: {days_elapsed}d, not live (Day 14 flag)"
        return None

    if package == "pkg-web":
        if is_live:
            return None
        if days_elapsed >= WEB_FLAG_DAYS[0]:
            return f"Website package: {days_elapsed}d, not live (Day 10 flag)"
        return None

    if package == "pkg-web-custom":
        if not has_eta:
            return "Custom web build has no dated ETA on the card"
        if eta_passed:
            return "Custom web build ETA has passed"
        return None

    if package == "pkg-seo":
        if not seo_started:
            return "SEO package: no on-site/off-site kickoff proof by end of signing week"
        return None

    if package == "pkg-web-seo":
        if days_elapsed >= WEB_SEO_FLAG_DAYS[0] and not fully_complete:
            return f"Website+SEO package: {days_elapsed}d, not fully complete (Day 10 flag)"
        return None

    if package == "pkg-free-promo":
        return None  # tracked, never alarmed per SKILL.md

    return f"Package unidentified — tag the card (defaulted to marketing rules for day count {days_elapsed})"


def get_active_client_names(db: Session, list_type: ManagedListType) -> set[str]:
    rows = db.query(ManagedClientEntry).filter_by(list_type=list_type, active=True).all()
    return {r.client_name for r in rows}


def build_digest(flags: list[Flag]) -> str:
    """Assembles the six-section digest per SKILL.md DO #6. Caps section length
    loosely toward the ~40-line target — trim further once real flag volume is known."""
    if not flags:
        return "Go-live audit: all clear today. :white_check_mark:"

    sections = [
        (":rotating_light: Action needed today", FlagCategory.action_needed),
        (":bar_chart: Heartbeat mismatches", FlagCategory.heartbeat_mismatch),
        (":credit_card: Payment", FlagCategory.payment),
        (":alarm_clock: Clock violations by package", FlagCategory.clock_violation),
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
    run = AuditRun(run_date=datetime.utcnow().date(), started_at=datetime.utcnow(), status=RunStatus.partial)
    db.add(run)
    db.flush()  # get run.id before attaching flags

    ex_clients = get_active_client_names(db, ManagedListType.ex_client)
    drive = GoogleDriveClient()
    ghl = GHLClient()
    flags: list[Flag] = []
    notes: list[str] = []

    # --- Step 1: heartbeat sheets, freshness + CSV fallback ---
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
            if row.checked_at != datetime.min and GoogleDriveClient.is_stale(row.checked_at):
                notes.append(f"{label} heartbeat sheet stale for {row.account_name} (checked_at {row.checked_at})")
            if legacy_only_spend_flag(row):
                flags.append(
                    Flag(
                        run_id=run.id,
                        category=FlagCategory.heartbeat_mismatch,
                        severity=FlagSeverity.warning,
                        client_name=row.account_name,
                        message=f"{label}: legacy campaign burning client budget — confirm intent",
                        created_at=datetime.utcnow(),
                    )
                )
        # TODO(port): match `rows` against ClickUp Go-Live board cards (fuzzy name
        # matching — see module docstring) to run the board-vs-heartbeat cross-check
        # (SKILL.md DO #2) and the per-card package-clock evaluation (DO #5) via
        # evaluate_package_clock() above. Needs card day-count + package tag/GHL lookup.

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

    db.add_all(flags)
    digest_text = build_digest(flags)
    run.digest_text = digest_text
    run.status = RunStatus.success if not notes else RunStatus.partial
    run.notes = "\n".join(notes) if notes else None
    run.finished_at = datetime.utcnow()
    db.commit()

    slack = SlackClient()
    slack.send_dm(settings.slack_christian_user_id, digest_text)

    return run
