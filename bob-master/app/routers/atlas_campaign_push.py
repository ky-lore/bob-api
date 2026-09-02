"""
Manual-trigger endpoint for pushing real campaign spend (Google Ads + Meta)
INTO Atlas -- see app/tasks/atlas_campaign_push.py. Manual-trigger only for
now (Bob, 2026-09-02): this is bob-master's first-ever write into a live
external system Atlas itself serves execs from, so nothing here runs on the
unattended daily cron yet -- review a real (or dry-run) trigger's output
first. Same job_id/poll shape as the daily-go-live-audit trigger (see
app/main.py, app/tasks/job_tracker.py) since a full run over every account
can take a while. dry_run defaults to True -- a bare POST with no query
params never writes to Atlas.
"""
from fastapi import APIRouter, HTTPException, Query

from app.tasks.atlas_campaign_push import push_campaign_spend
from app.tasks.job_tracker import get_job, start_job

router = APIRouter()


@router.post("/tasks/atlas-campaign-push/run")
def trigger_atlas_campaign_push(
    dry_run: bool = Query(default=True, description="Build every payload without POSTing to Atlas"),
    limit: int | None = Query(default=None, description="Cap the account universe (smoke-testing only)"),
) -> dict:
    job_id = start_job(lambda: {"dry_run": dry_run, "results": push_campaign_spend(limit=limit, dry_run=dry_run)})
    return {"job_id": job_id, "job_status": "running", "dry_run": dry_run}


@router.get("/tasks/atlas-campaign-push/run/{job_id}")
def get_atlas_campaign_push_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if job["status"] == "running":
        return {"job_status": "running"}
    if job["status"] == "error":
        return {"job_status": "error", "error": job["error"]}
    return {"job_status": "done", **job["result"]}
