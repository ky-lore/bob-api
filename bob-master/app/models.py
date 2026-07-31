import enum
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Text
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
