"""
Manual task-creation endpoint for Command Center's "create action task" UI
(2026-09-28) -- the one write-path Atlas calls INTO bob-master (every other
Atlas<->Bob integration in this app is Bob pushing OUT to Atlas). Guarded
the same way every other internal admin mutation here is (see
atlas_report.py's _require_admin_password/_ADMIN_OVERRIDE_PASSWORD) --
deliberately weak, matches the explicit "completely internal facing" call
Chris made for that endpoint; not re-litigated here.
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.tasks.admin_task_create import create_admin_task

router = APIRouter()

_ADMIN_PASSWORD = "wasp"


def _require_admin_password(x_admin_password: str | None = Header(default=None)) -> None:
    if x_admin_password != _ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="invalid or missing admin password")


class _CreateAdminTaskRequest(BaseModel):
    atlas_id: str
    title: str
    assignee_email: str | None = None
    dueDate: str | None = None
    startDate: str | None = None


@router.post("/admin-tasks/create", dependencies=[Depends(_require_admin_password)])
def create_admin_task_endpoint(payload: _CreateAdminTaskRequest) -> dict:
    try:
        task = create_admin_task(
            payload.atlas_id,
            payload.title,
            assignee_email=payload.assignee_email,
            due_date=payload.dueDate,
            start_date=payload.startDate,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "task": task}
