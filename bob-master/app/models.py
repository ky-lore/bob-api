import enum
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RunStatus(str, enum.Enum):
    success = "success"
    partial = "partial"   # e.g. a heartbeat sheet was unreadable by both methods
    failed = "failed"


class FlagCategory(str, enum.Enum):
    action_needed = "action_needed"
    heartbeat_mismatch = "heartbeat_mismatch"
    payment = "payment"
    clock_violation = "clock_violation"
    new_deal = "new_deal"
    went_live = "went_live"
    # "Ads off — who's dark and why" buckets, per the reference dashboard
    # (golive-pipeline-dashboard.pdf) — see app/tasks/ads_off_classification.py.
    ads_off_should_be_on = "ads_off_should_be_on"
    ads_off_zero_spend = "ads_off_zero_spend"
    ads_off_unsettled = "ads_off_unsettled"
    ads_off_verified_off = "ads_off_verified_off"


class FlagSeverity(str, enum.Enum):
    info = "info"
    warning = "warning"
    urgent = "urgent"


class ManagedListType(str, enum.Enum):
    watchlist = "watchlist"    # replaces the hardcoded named-client watch-list in SKILL.md
    ex_client = "ex_client"    # replaces the hardcoded ex-client exclusion list in SKILL.md
    # Human-confirmed name pairs for real cross-system near-duplicates (e.g.
    # "Roof City Inc - CC" on the heartbeat sheet vs "Roof City Professionals"
    # on the ClickUp board) that no fuzzy-matching algorithm resolves on its
    # own. client_name = canonical/heartbeat-side name, note = the ClickUp-side
    # alias. See app/tasks/matching.py.
    alias = "alias"


class AuditRun(Base):
    """One row per daily-go-live-audit execution. This is what makes 'past days
    tracked' possible on the dashboard — the original Cowork version had no
    history, only a same-day digest + a self-overwriting artifact."""

    __tablename__ = "audit_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_date: Mapped[date] = mapped_column(Date, index=True, unique=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus))
    digest_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # the Slack DM body sent
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # e.g. which heartbeat sheet failed
    # JSON blob: {"stat_tiles": {...}, "rows": [{"account":.., "day":.., "stage":.., "blocking":..}]}.
    # Computed once per run (stat tiles deterministically, "blocking" narrative
    # via one batched LLM call) and stored — same reasoning as digest_text:
    # don't recompute/re-call the LLM every time someone loads the dashboard.
    dashboard_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # JSON blob: {account_name: {"clickup_ok":, "clickup_comment_count":,
    # "clickup_error":, "slack_channel_matched":, "slack_ok":,
    # "slack_message_count":, "slack_error":}} — per-account diagnostics for
    # the MVP rich-context gather (account_context_gather.py), surfaced via
    # the manual-trigger endpoint's response body (see main.py). Separate
    # from dashboard_json since this isn't dashboard content, just run health.
    context_gather_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    flags: Mapped[list["Flag"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Flag(Base):
    """One row per finding surfaced in a run — action items, heartbeat mismatches,
    clock violations, etc. Maps directly to the digest sections in SKILL.md DO #6."""

    __tablename__ = "flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("audit_runs.id"))
    category: Mapped[FlagCategory] = mapped_column(Enum(FlagCategory))
    severity: Mapped[FlagSeverity] = mapped_column(Enum(FlagSeverity), default=FlagSeverity.warning)
    client_name: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    evidence_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    unverified: Mapped[bool] = mapped_column(Boolean, default=False)  # per ACCURACY RULES §1
    created_at: Mapped[datetime] = mapped_column(DateTime)

    run: Mapped[AuditRun] = relationship(back_populates="flags")


class ActionItemCheckoff(Base):
    """A checked-off LLM-recommended action item for one account on one run
    (Bob, 2026-08-11). Scoped to (run_id, account_name), not a stable
    cross-day identity -- the recommended action itself is freshly generated
    by the LLM every run and its wording can drift day to day even for the
    same underlying issue, so there's no fuzzy-matching attempt to carry a
    checkmark forward (this codebase deliberately dropped all fuzzy-matching
    with the Atlas migration, see daily_go_live_audit.py's module docstring).
    Existence of a row = checked; there's nothing to un-delete, so unchecking
    just deletes the row. No login yet (see admin.py), so this is
    unattributed -- anyone with the dashboard URL can toggle it."""

    __tablename__ = "action_item_checkoffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("audit_runs.id"))
    account_name: Mapped[str] = mapped_column(String(255))
    checked_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (UniqueConstraint("run_id", "account_name", name="uq_action_item_checkoff_run_account"),)


class ZoomCallRecord(Base):
    """One row per Zoom cloud recording that had a transcript, pulled by the
    daily Zoom call sync (2026-09-17) -- see app/tasks/zoom_call_sync.py.

    meeting_uuid is Zoom's own unique meeting-instance identifier (unlike
    meeting_number/id, which repeats across a recurring/PMI meeting's
    instances) -- the natural dedup key, so a day's sync just skips any uuid
    already present rather than needing a separate watermark/state table.

    atlas_account_id/match_confidence come from fuzzy-matching the meeting
    topic against Atlas company names (see app/tasks/account_name_matching.py)
    -- Zoom has no Atlas ID field to join on directly. atlas_account_id is
    Atlas's own permanent account id (2026-09-17, "Atlas remains the holy
    grail source of truth" -- not a second copy of identity bob-master
    maintains itself); matched_company_name is a denormalized label
    snapshotted at match time, purely so a row reads clearly without a
    join back to Atlas -- same convention as Flag.client_name elsewhere in
    this file, not a second source of truth for the name. No separate
    "accounts" table: this Postgres has never had one (every account
    reference elsewhere in this file, e.g. Flag.client_name, is a plain
    string too), and Atlas is external to it anyway -- a plain indexed
    atlas_account_id column gets the same "drill into one account's
    transcripts by date" query pattern a nested table would, without
    needing to maintain a redundant local copy of Atlas's account list.
    Always stored, even at low confidence (unlike the ClickUp folder
    re-bridge's upload CSV, nothing here gets bulk-applied anywhere
    automatically, so there's no reason to throw away a low-confidence
    guess -- a human or a later query can filter on confidence)."""

    __tablename__ = "zoom_call_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_uuid: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    host_email: Mapped[str] = mapped_column(String(255))
    topic: Mapped[str] = mapped_column(Text)
    start_time: Mapped[datetime] = mapped_column(DateTime)
    atlas_account_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    matched_company_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    match_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    transcript_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pulled_at: Mapped[datetime] = mapped_column(DateTime)


class ManagedClientEntry(Base):
    """Exec-editable replacement for the two hardcoded lists in SKILL.md: the
    named-client watch-list (Sierra Trimlight, 5blox, etc.) and the ex-client
    exclusion list (Joa Brothers, Paradise Concrete, etc.). Edited via the
    in-app admin table (routers/admin.py) instead of a prompt/code edit."""

    __tablename__ = "managed_client_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    list_type: Mapped[ManagedListType] = mapped_column(Enum(ManagedListType))
    client_name: Mapped[str] = mapped_column(String(255), index=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class AtlasReportRun(Base):
    """One row per app/tasks/atlas_report.py run (2026-09-18) -- the
    persistent counterpart to job_tracker's in-memory status, same
    "job_tracker answers is-it-running, the DB row is the real durable
    result" split AuditRun already established for the daily audit (see
    job_tracker.py's docstring). Needed because a full ~148-account run
    takes well past Railway's ~300s gateway timeout on a synchronous
    request, AND because job_tracker itself doesn't survive a redeploy
    (confirmed the hard way with the daily audit, 2026-09-18) -- without
    this row, a slow run that outlives a redeploy would leave nothing to
    show for it. GET .../latest always serves this table directly, never
    job_tracker, so the last completed run stays visible regardless of
    process restarts.

    report_json is the same {"count", "accounts", "narrative_batches"} shape
    build_atlas_report's caller already returns over HTTP -- stored verbatim
    rather than normalized into columns, since nothing here needs to query
    inside it yet (see AuditRun.dashboard_json for the same convention)."""

    __tablename__ = "atlas_report_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    limit_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    report_json: Mapped[str] = mapped_column(Text)


class AccountHealthOverride(Base):
    """A human's manual correction to Pulse's LLM-derived health chip
    (2026-09-21, Bob: "some are under-flagged and some are over-flagged").
    One row per account (unique on atlas_id) -- setting a new override for
    an account already overridden just replaces the row rather than keeping
    history; this is a rudimentary correction mechanism, not an audit trail.
    Applied in app/tasks/atlas_report.py as the last step before a run's
    health is persisted, so it wins over whatever the LLM inferred THAT run
    and every run after, until cleared (DELETE .../overrides/{atlas_id}).

    Keyed on atlas_id, not company_name, same reasoning as ZoomCallRecord --
    Atlas's own id is the stable join key, never a display string.
    company_name is denormalized purely so the admin.py-style list view (if
    one gets built later) doesn't need a join back to Atlas to be readable.

    No real auth on the write endpoints -- a single shared password checked
    against Settings.admin_override_password, stored client-side in
    sessionStorage, not a session/user system. Same trust-level tier as
    every other admin-ish endpoint in this app today (see admin.py's
    docstring) -- Bob's explicit call, not an oversight."""

    __tablename__ = "account_health_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    atlas_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(255))
    health: Mapped[str] = mapped_column(String(32))
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    set_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
