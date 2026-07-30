"""
Exec-editable watchlist / ex-client admin table — replaces the hardcoded lists in
SKILL.md. Plain server-rendered form for now; no auth wired in yet (TODO before
this is exposed beyond Railway's private networking — this edits data that feeds
a daily report to Chris, don't leave it open).
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ManagedClientEntry, ManagedListType

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


@router.get("/watchlist", response_class=HTMLResponse)
def list_entries(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    entries = db.query(ManagedClientEntry).filter_by(active=True).order_by(ManagedClientEntry.list_type).all()
    return templates.TemplateResponse(request, "admin.html", {"entries": entries})


@router.post("/watchlist")
def add_entry(
    client_name: str = Form(...),
    list_type: ManagedListType = Form(...),
    note: str = Form(""),
    created_by: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    now = datetime.utcnow()
    db.add(
        ManagedClientEntry(
            client_name=client_name,
            list_type=list_type,
            note=note or None,
            created_by=created_by or None,
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()
    return RedirectResponse(url="/admin/watchlist", status_code=303)


@router.post("/watchlist/{entry_id}/deactivate")
def deactivate_entry(entry_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    entry = db.get(ManagedClientEntry, entry_id)
    if entry:
        entry.active = False
        entry.updated_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(url="/admin/watchlist", status_code=303)
