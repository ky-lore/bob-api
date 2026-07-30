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

from app.config import get_settings
from app.db import get_db
from app.integrations.clickup import ClickUpClient
from app.integrations.google_drive import GoogleDriveClient
from app.integrations.slack import SlackClient
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


@router.post("/slack/join-public-channels")
def join_public_channels() -> dict:
    """One-off/rerunnable: has the bot self-join every public channel it's not
    already in. Requires the channels:join bot scope. Public channels only —
    private channels still need a human invite, see app/integrations/slack.py."""
    return SlackClient().join_all_public_channels()


@router.get("/heartbeat/headers")
def heartbeat_headers() -> dict:
    """Debug-only: dumps row 1 (the header row) of both heartbeat sheets, so
    parse_heartbeat_rows()'s column-matching can be checked against the real
    sheet without anyone having to go dig through the spreadsheet by hand."""
    settings = get_settings()
    drive = GoogleDriveClient()
    google_ads_rows = drive.read_sheet_values(
        settings.drive_google_ads_heartbeat_file_id, settings.drive_google_ads_heartbeat_tab
    )
    meta_rows = drive.read_sheet_values(
        settings.drive_meta_heartbeat_file_id, settings.drive_meta_heartbeat_tab
    )
    return {
        "google_ads_header": google_ads_rows[0] if google_ads_rows else [],
        "meta_header": meta_rows[0] if meta_rows else [],
    }


def _clickup_list_sample(list_id: str) -> dict:
    """Shared by every clickup/*-sample debug endpoint below: real board data,
    so fuzzy-matching/stage-aware logic gets designed against actual card
    shape (statuses, tags, custom fields, description text) instead of
    guessed at — same reasoning as the heartbeat-headers endpoint, applied
    to each ClickUp list as it gets connected."""
    clickup = ClickUpClient()
    data = clickup.get_list_tasks(list_id, include_closed=True)
    tasks = data.get("tasks", [])

    cards = [
        {
            "id": t.get("id"),
            "name": t.get("name"),
            "status": (t.get("status") or {}).get("status"),
            "tags": [tag.get("name") for tag in t.get("tags", [])],
            "date_created": t.get("date_created"),
            "date_updated": t.get("date_updated"),
            "description": t.get("description"),
            "custom_fields": [
                {"name": cf.get("name"), "value": cf.get("value")}
                for cf in t.get("custom_fields", [])
                if cf.get("value") not in (None, "", [])
            ],
        }
        for t in tasks
    ]

    return {
        "total_cards_this_page": len(tasks),
        "last_page": data.get("last_page", True),
        "cards": cards,
        "raw_first_task": tasks[0] if tasks else None,
    }


@router.get("/clickup/go-live-sample")
def clickup_go_live_sample() -> dict:
    return _clickup_list_sample(get_settings().clickup_go_live_list_id)


@router.get("/clickup/web-build-sample")
def clickup_web_build_sample() -> dict:
    return _clickup_list_sample(get_settings().clickup_web_build_list_id)


@router.get("/clickup/retention-sample")
def clickup_retention_sample() -> dict:
    return _clickup_list_sample(get_settings().clickup_retention_list_id)


@router.get("/sheets/tv-board-feed")
def tv_board_feed_sample() -> dict:
    """Debug-only: the TV board feed sheet — config has had a file ID since
    the first scaffold, but nothing has ever actually read it. Need to see
    real tab names (especially the "Ignore" tab SKILL.md references) before
    wiring it into ex-client filtering."""
    settings = get_settings()
    drive = GoogleDriveClient()
    try:
        tab_names = drive.list_tabs(settings.drive_tv_board_feed_file_id)
    except Exception as exc:
        return {"error": str(exc)}

    sample = {}
    for tab in tab_names:
        rows = drive.read_sheet_values(settings.drive_tv_board_feed_file_id, tab)
        sample[tab] = {"header": rows[0] if rows else [], "row_count": max(len(rows) - 1, 0)}

    return {"tabs": tab_names, "sample": sample}
