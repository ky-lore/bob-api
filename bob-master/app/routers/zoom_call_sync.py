"""
Manual-trigger endpoint for the daily Zoom call transcript sync -- see
app/tasks/zoom_call_sync.py. Manual-trigger only for now, not wired into the
scheduler: same rollout posture as atlas_campaign_push (review a real run's
output before anything runs unattended). Same job_id/poll shape as every
other background task in this app (see app/tasks/job_tracker.py).
"""
from datetime import date

from fastapi import APIRouter, HTTPException, Query

from app.db import get_session_factory
from app.tasks.job_tracker import get_job, start_job
from app.tasks.zoom_call_sync import backfill_zoom_calls, sync_zoom_calls

router = APIRouter()


def _run_sync(target_date: date | None) -> dict:
    db = get_session_factory()()
    try:
        return sync_zoom_calls(db, target_date=target_date)
    finally:
        db.close()


def _run_backfill(days: int) -> dict:
    db = get_session_factory()()
    try:
        return backfill_zoom_calls(db, days=days)
    finally:
        db.close()


@router.post("/tasks/zoom-call-sync/run")
def trigger_zoom_call_sync(
    target_date: date | None = Query(default=None, description="Defaults to yesterday (UTC)"),
) -> dict:
    job_id = start_job(lambda: _run_sync(target_date))
    return {"job_id": job_id, "job_status": "running"}


@router.post("/tasks/zoom-call-sync/backfill")
def trigger_zoom_call_backfill(
    days: int = Query(default=30, description="How many days back to pull, chunked safely under Zoom's silent ~1-month range clamp"),
) -> dict:
    """One-off wide pull (e.g. "populate the past month") -- polls at the
    same GET .../run/{job_id} route below, job_tracker doesn't care which
    task produced a given job_id."""
    job_id = start_job(lambda: _run_backfill(days))
    return {"job_id": job_id, "job_status": "running"}


@router.get("/tasks/zoom-call-sync/run/{job_id}")
def get_zoom_call_sync_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if job["status"] == "running":
        return {"job_status": "running"}
    if job["status"] == "error":
        return {"job_status": "error", "error": job["error"]}
    return {"job_status": "done", **job["result"]}
