"""
GET endpoint for Atlas to pull the consolidated per-account report (see
app/tasks/atlas_report.py) — presumably via a daily cron on Atlas's side.
Recomputes live on every call; no caching layer, per Bob (2026-08-06):
"inefficiency isn't a big deal, it'll run on a daily basis."

Async trigger/poll/latest added 2026-09-18 for the full, unbounded account
universe (~148 accounts, ~9s/account with Zoom+Meta+ClickUp+Slack+LLM all in
the mix) — a synchronous GET over that many accounts reliably outruns
Railway's ~300s gateway timeout (confirmed the hard way: a real unlimited
pull 502'd at exactly 300s). Same job_tracker.start_job/get_job pattern
main.py's daily-go-live-audit trigger already uses, PLUS real persistence
(AtlasReportRun, see app/models.py) since job_tracker itself doesn't survive
a Railway redeploy (also confirmed the hard way, same day, on that same
daily-audit trigger) -- Bob was explicit this run should stay visible even
if a redeploy or restart happens mid-run or right after. GET .../latest
always reads AtlasReportRun directly, independent of job_tracker/poll state,
so it's a stable link regardless of process restarts. The original
synchronous GET is left as-is for Atlas's own cron (presumably a longer
timeout budget than a browser/curl) and for quick `?limit=N` smoke tests.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db, get_session_factory
from app.models import AtlasReportRun
from app.tasks.atlas_report import build_atlas_report, run_and_store_atlas_report
from app.tasks.job_tracker import get_job, start_job

router = APIRouter()


@router.get("/reports/atlas-account-status")
def get_atlas_account_status_report(
    limit: int | None = Query(default=None, description="Cap the account universe (smoke-testing only)"),
    db: Session = Depends(get_db),
) -> dict:
    records, narrative_batches = build_atlas_report(db=db, limit=limit)
    return {"count": len(records), "accounts": records, "narrative_batches": narrative_batches}


def _run_to_response(run: AtlasReportRun) -> dict:
    data = json.loads(run.report_json)
    return {"run_id": run.id, "run_at": run.run_at.isoformat(), **data}


def _run_atlas_report_job(limit: int | None) -> dict:
    """Runs on job_tracker's background thread -- needs its own DB session
    since the request's is long gone by the time this executes (same
    reasoning as main.py's _run_daily_go_live_audit_and_summarize)."""
    db = get_session_factory()()
    try:
        run = run_and_store_atlas_report(db, limit=limit)
        return _run_to_response(run)
    finally:
        db.close()


@router.post("/reports/atlas-account-status/run")
def trigger_atlas_report(
    limit: int | None = Query(default=None, description="Cap the account universe (smoke-testing only)"),
) -> dict:
    """Manual-trigger endpoint for the full report, run in the background.
    Returns a job_id immediately instead of blocking (see module docstring
    for why). Poll GET .../run/{job_id} for status, or GET .../latest once
    it's done -- the latter survives a redeploy mid-run, the former doesn't."""
    job_id = start_job(lambda: _run_atlas_report_job(limit))
    return {"job_id": job_id, "job_status": "running"}


@router.get("/reports/atlas-account-status/run/{job_id}")
def get_atlas_report_run_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if job["status"] == "running":
        return {"job_status": "running"}
    if job["status"] == "error":
        return {"job_status": "error", "error": job["error"]}
    return {"job_status": "done", **job["result"]}


@router.get("/reports/atlas-account-status/latest")
def get_latest_atlas_report(db: Session = Depends(get_db)) -> dict:
    """Always reads the most recent AtlasReportRun row directly from Postgres
    -- a stable link to the last completed run regardless of job_tracker
    state or process restarts (see module docstring)."""
    run = db.query(AtlasReportRun).order_by(AtlasReportRun.run_at.desc()).first()
    if run is None:
        return {"run_id": None, "run_at": None, "count": 0, "accounts": [], "narrative_batches": []}
    return _run_to_response(run)
