"""
Manual-trigger endpoint for the standup action-item sync -- see
app/tasks/standup_action_items.py. Manual-trigger only for now, not wired
into the scheduler: same rollout posture as zoom_call_sync/atlas_campaign_push
(review a real run's output before anything runs unattended). Same job_id/poll
shape as every other background task in this app (see app/tasks/job_tracker.py).
No DB session involved here (unlike zoom_call_sync) -- this task only relays
ClickUp -> Atlas, it never persists anything in Bob's own Postgres.
"""
from fastapi import APIRouter, HTTPException, Query

from app.tasks.job_tracker import get_job, start_job
from app.tasks.standup_action_items import sync_standup_action_items

router = APIRouter()


@router.post("/tasks/standup-action-items/run")
def trigger_standup_action_items_sync(
    name_filter: str = Query(default="admin", description="Case-insensitive substring to match against ClickUp task names, workspace-wide"),
) -> dict:
    job_id = start_job(lambda: sync_standup_action_items(name_filter=name_filter))
    return {"job_id": job_id, "job_status": "running"}


@router.get("/tasks/standup-action-items/run/{job_id}")
def get_standup_action_items_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if job["status"] == "running":
        return {"job_status": "running"}
    if job["status"] == "error":
        return {"job_status": "error", "error": job["error"]}
    return {"job_status": "done", **job["result"]}
