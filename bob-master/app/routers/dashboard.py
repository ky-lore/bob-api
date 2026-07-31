"""
Daily priority list — the FastAPI replacement for the Cowork 'golive-pipeline-dashboard'
artifact, which had no export in this package and can't run outside Cowork anyway
(it called window.cowork.callMcpTool()). Primary content is AuditRun.dashboard_json
(stat tiles + LLM-narrated "what's blocking" rows, see tasks/dashboard_summary.py);
the raw flag list below it is kept as a transparent detail view underneath.
"""
import json
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AuditRun, FlagCategory

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _parse_dashboard_json(run: AuditRun | None) -> dict:
    empty = {"stat_tiles": {}, "rows": [], "narrative_error": None}
    if not run or not run.dashboard_json:
        return empty
    try:
        parsed = json.loads(run.dashboard_json)
    except (ValueError, TypeError):
        return empty
    # .get() with fallback, not direct indexing — older stored runs predate
    # the narrative_error key and shouldn't 500 the whole page over it.
    return {
        "stat_tiles": parsed.get("stat_tiles", {}),
        "rows": parsed.get("rows", []),
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


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def latest_dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    latest = db.query(AuditRun).order_by(AuditRun.run_date.desc()).first()
    history = db.query(AuditRun).order_by(AuditRun.run_date.desc()).limit(30).all()
    dashboard_data = _parse_dashboard_json(latest)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "run": latest,
            "history": history,
            "sections": _grouped_sections(latest),
            "stat_tiles": dashboard_data["stat_tiles"],
            "rows": dashboard_data["rows"],
            "narrative_error": dashboard_data["narrative_error"],
        },
    )


@router.get("/dashboard/{run_date}", response_class=HTMLResponse)
def dashboard_for_date(run_date: date, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    run = db.query(AuditRun).filter_by(run_date=run_date).first()
    history = db.query(AuditRun).order_by(AuditRun.run_date.desc()).limit(30).all()
    dashboard_data = _parse_dashboard_json(run)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "run": run,
            "history": history,
            "sections": _grouped_sections(run),
            "stat_tiles": dashboard_data["stat_tiles"],
            "rows": dashboard_data["rows"],
            "narrative_error": dashboard_data["narrative_error"],
        },
    )
