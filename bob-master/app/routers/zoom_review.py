"""
Manual review/assignment UI for Zoom call transcripts stored in
`zoom_call_records` (see app/tasks/zoom_call_sync.py) that the fuzzy
topic-matcher (app/tasks/account_name_matching.py) either missed
(atlas_account_id is None) or got wrong. A human pick here is written
straight into atlas_account_id/matched_company_name -- the same columns
every existing consumer (_add_zoom_context in app/tasks/atlas_report.py,
app/tasks/daily_go_live_audit.py) already filters on -- so it takes effect
on the very next context gather / Pulse blend with no other code changes,
and re-sync can never clobber it (sync_zoom_calls/backfill_zoom_calls only
ever touch meeting_uuids not already in the table).

Plain server-rendered form, same convention as app/routers/admin.py --
no auth wired in yet, same as that router (internal-only, Railway private
networking).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations.atlas_client import AtlasClient
from app.models import ZoomCallRecord

router = APIRouter(prefix="/admin/zoom-calls")
templates = Jinja2Templates(directory="app/templates")

_LIST_LIMIT = 200


def _active_atlas_accounts() -> tuple[list[dict], str | None]:
    """(accounts, error) -- soft-fails like google_ads_error/meta_ads_error
    elsewhere in this app, since a live Atlas API hiccup shouldn't 500 the
    whole review page, just leave the assign dropdown empty with a banner."""
    try:
        accounts = [a for a in AtlasClient().get_all_accounts() if a.get("isActive")]
        accounts.sort(key=lambda a: (a.get("companyName") or "").lower())
        return accounts, None
    except Exception as exc:
        return [], str(exc)


@router.get("", response_class=HTMLResponse)
def list_calls(request: Request, filter: str = "unassigned", q: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    query = db.query(ZoomCallRecord)
    if filter == "unassigned":
        query = query.filter(ZoomCallRecord.atlas_account_id.is_(None))
    elif filter == "auto_matched":
        query = query.filter(
            ZoomCallRecord.atlas_account_id.is_not(None),
            ZoomCallRecord.manually_assigned.is_not(True),
        )
    elif filter == "manually_assigned":
        query = query.filter(ZoomCallRecord.manually_assigned.is_(True))
    # filter == "all" -> no extra clause

    if q:
        like = f"%{q}%"
        query = query.filter(
            (ZoomCallRecord.topic.ilike(like))
            | (ZoomCallRecord.host_email.ilike(like))
            | (ZoomCallRecord.matched_company_name.ilike(like))
        )

    calls = query.order_by(ZoomCallRecord.start_time.desc()).limit(_LIST_LIMIT).all()
    accounts, atlas_fetch_error = _active_atlas_accounts()

    return templates.TemplateResponse(
        request,
        "zoom_calls.html",
        {
            "calls": calls,
            "accounts": accounts,
            "atlas_fetch_error": atlas_fetch_error,
            "filter": filter,
            "q": q,
            "list_limit": _LIST_LIMIT,
        },
    )


@router.post("/{call_id}/assign")
def assign_call(
    call_id: int,
    atlas_account_id: str = Form(...),
    assigned_by: str = Form(""),
    filter: str = Form("unassigned"),
    q: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    call = db.get(ZoomCallRecord, call_id)
    if call is None:
        return RedirectResponse(url=_back_url(filter, q), status_code=303)

    accounts, _ = _active_atlas_accounts()
    company_name = next((a.get("companyName") for a in accounts if a.get("id") == atlas_account_id), None)

    call.atlas_account_id = atlas_account_id
    call.matched_company_name = company_name
    call.manually_assigned = True
    call.assigned_by = assigned_by or None
    call.assigned_at = datetime.now(timezone.utc)
    db.add(call)
    db.commit()

    return RedirectResponse(url=_back_url(filter, q), status_code=303)


@router.post("/{call_id}/clear")
def clear_call(
    call_id: int,
    filter: str = Form("unassigned"),
    q: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    call = db.get(ZoomCallRecord, call_id)
    if call is not None:
        call.atlas_account_id = None
        call.matched_company_name = None
        call.match_confidence = None
        call.manually_assigned = False
        call.assigned_by = None
        call.assigned_at = None
        db.add(call)
        db.commit()

    return RedirectResponse(url=_back_url(filter, q), status_code=303)


def _back_url(filter: str, q: str) -> str:
    from urllib.parse import urlencode

    params = {"filter": filter}
    if q:
        params["q"] = q
    return f"/admin/zoom-calls?{urlencode(params)}"
