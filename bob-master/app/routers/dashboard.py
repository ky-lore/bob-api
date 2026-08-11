"""
Daily priority list — the FastAPI replacement for the Cowork 'golive-pipeline-dashboard'
artifact, which had no export in this package and can't run outside Cowork anyway
(it called window.cowork.callMcpTool()). Primary content is AuditRun.dashboard_json:
stat tiles, an "accounts overview" narrative table covering every matched go-live-list
account (live or not — fully macro as of 2026-07-31, see dashboard_summary.py), "ads
off" breakdown, new deals, went live. The raw flag list below it is kept as a
transparent detail view underneath — not part of the reference dashboard, but
useful for us as developers.
"""
import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations.anthropic_client import NO_ACTION_NEEDED
from app.models import ActionItemCheckoff, AuditRun, FlagCategory

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_EMPTY_DASHBOARD_DATA = {
    "stat_tiles": {},
    "accounts_overview": [],
    "accounts_chart": [],
    "ads_off": {"should_be_on_but_dark": [], "campaigns_on_zero_spend": [], "unsettled": [], "verified_off": []},
    "new_deals": [],
    "went_live": [],
    "web_builds": [],
    "narrative_error": None,
}


def _parse_dashboard_json(run: AuditRun | None) -> dict:
    if not run or not run.dashboard_json:
        return dict(_EMPTY_DASHBOARD_DATA)
    try:
        parsed = json.loads(run.dashboard_json)
    except (ValueError, TypeError):
        return dict(_EMPTY_DASHBOARD_DATA)
    # .get() with fallbacks throughout — older stored runs predate some of
    # these keys and shouldn't 500 the whole page over it.
    return {
        "stat_tiles": parsed.get("stat_tiles", {}),
        "accounts_overview": parsed.get("accounts_overview", []),
        "accounts_chart": parsed.get("accounts_chart", []),
        "ads_off": {
            **_EMPTY_DASHBOARD_DATA["ads_off"],
            **parsed.get("ads_off", {}),
        },
        "new_deals": parsed.get("new_deals", []),
        "went_live": parsed.get("went_live", []),
        "web_builds": parsed.get("web_builds", []),
        "narrative_error": parsed.get("narrative_error"),
    }


# Same order as build_digest()'s SECTIONS in daily_go_live_audit.py — Jinja's
# groupby filter sorts alphabetically, which doesn't match the intended report
# order, so it's built explicitly here instead of left to the template.
_SECTION_ORDER = [
    ("Action needed today", FlagCategory.action_needed),
    ("Heartbeat mismatches", FlagCategory.heartbeat_mismatch),
    ("Payment", FlagCategory.payment),
    ("Clock violations by package", FlagCategory.clock_violation),
    ("New deals", FlagCategory.new_deal),
    ("Went live", FlagCategory.went_live),
    ("Ads off — should be ON but dark", FlagCategory.ads_off_should_be_on),
    ("Ads off — campaigns on, $0 spend", FlagCategory.ads_off_zero_spend),
    ("Ads off — unsettled payment", FlagCategory.ads_off_unsettled),
    ("Ads off — verified off", FlagCategory.ads_off_verified_off),
]


def _grouped_sections(run: AuditRun | None) -> list[tuple[str, list]]:
    if not run:
        return []
    sections = []
    for title, category in _SECTION_ORDER:
        items = [f for f in run.flags if f.category == category]
        if items:
            sections.append((title, items))
    return sections


def _checked_action_items(db: Session, run: AuditRun | None) -> set[str]:
    """Account names with a checked-off recommended action for this specific
    run -- see ActionItemCheckoff's docstring for why this is scoped per-run,
    not carried across days."""
    if not run:
        return set()
    rows = db.query(ActionItemCheckoff.account_name).filter_by(run_id=run.id).all()
    return {r[0] for r in rows}


def _dashboard_context(db: Session, run: AuditRun | None, history: list[AuditRun]) -> dict:
    dashboard_data = _parse_dashboard_json(run)
    checked = _checked_action_items(db, run)
    dashboard_data["accounts_overview"] = [
        {**a, "action_checked": a["account"] in checked} for a in dashboard_data["accounts_overview"]
    ]
    return {
        "run": run,
        "history": history,
        "sections": _grouped_sections(run),
        "no_action_needed": NO_ACTION_NEEDED,
        **dashboard_data,
    }


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def latest_dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    latest = db.query(AuditRun).order_by(AuditRun.run_date.desc()).first()
    history = db.query(AuditRun).order_by(AuditRun.run_date.desc()).limit(30).all()
    return templates.TemplateResponse(request, "dashboard.html", _dashboard_context(db, latest, history))


@router.get("/dashboard/{run_date}", response_class=HTMLResponse)
def dashboard_for_date(run_date: date, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    run = db.query(AuditRun).filter_by(run_date=run_date).first()
    history = db.query(AuditRun).order_by(AuditRun.run_date.desc()).limit(30).all()
    return templates.TemplateResponse(request, "dashboard.html", _dashboard_context(db, run, history))


class _ActionItemToggleRequest(BaseModel):
    run_id: int
    account_name: str


@router.post("/dashboard/action-items/toggle")
def toggle_action_item(payload: _ActionItemToggleRequest, db: Session = Depends(get_db)) -> dict:
    """Checks/unchecks one account's recommended action for one run. No auth
    yet (see admin.py) -- anyone with the dashboard URL can toggle this, same
    trust level as the rest of this app's admin endpoints today. Existence
    of a row = checked (see ActionItemCheckoff), so unchecking just deletes it."""
    existing = (
        db.query(ActionItemCheckoff)
        .filter_by(run_id=payload.run_id, account_name=payload.account_name)
        .first()
    )
    if existing:
        db.delete(existing)
        db.commit()
        return {"checked": False}
    db.add(ActionItemCheckoff(run_id=payload.run_id, account_name=payload.account_name, checked_at=datetime.utcnow()))
    db.commit()
    return {"checked": True}
