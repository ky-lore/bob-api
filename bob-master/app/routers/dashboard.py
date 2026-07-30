"""
Daily priority list — the FastAPI replacement for the Cowork 'golive-pipeline-dashboard'
artifact, which had no export in this package and can't run outside Cowork anyway
(it called window.cowork.callMcpTool()). This version is intentionally plain HTML
for now; the interactive version is a follow-up, not this pass.
"""
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AuditRun

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def latest_dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    latest = db.query(AuditRun).order_by(AuditRun.run_date.desc()).first()
    history = db.query(AuditRun).order_by(AuditRun.run_date.desc()).limit(30).all()
    return templates.TemplateResponse(
        request, "dashboard.html", {"run": latest, "history": history}
    )


@router.get("/dashboard/{run_date}", response_class=HTMLResponse)
def dashboard_for_date(run_date: date, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    run = db.query(AuditRun).filter_by(run_date=run_date).first()
    history = db.query(AuditRun).order_by(AuditRun.run_date.desc()).limit(30).all()
    return templates.TemplateResponse(
        request, "dashboard.html", {"run": run, "history": history}
    )
