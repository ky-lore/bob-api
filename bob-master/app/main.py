from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from adspend.main import app as adspend_app
from app.db import get_session_factory, init_db
from app.routers import admin, atlas_report, dashboard
from app.scheduler import start_scheduler
from app.tasks.daily_go_live_audit import run_daily_go_live_audit
from app.tasks.job_tracker import get_job, start_job


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield


app = FastAPI(title="Bob", lifespan=lifespan)
app.include_router(dashboard.router)
app.include_router(admin.router)
app.include_router(atlas_report.router)
# adspend (2026-08-06) mounted under this same deployment rather than run as
# its own Railway service -- one base URL for every consumer, current and
# future, instead of managing several. Its code (adspend/) stays a
# self-contained package regardless (own config, own clients) so it could
# still be lifted into its own service later if that's ever actually needed.
app.mount("/adspend", adspend_app)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _run_daily_go_live_audit_and_summarize() -> dict:
    """Runs on job_tracker's background thread -- needs its own DB session
    since the request's is long gone by the time this executes. Same summary
    shape the trigger endpoint used to return directly, before a full-account
    run started outrunning client/proxy timeouts (see chat, 2026-08-11)."""
    db = get_session_factory()()
    try:
        run = run_daily_go_live_audit(db)
        dashboard_data = json.loads(run.dashboard_json) if run.dashboard_json else {}
        context_gather = json.loads(run.context_gather_json) if run.context_gather_json else {}
        return {
            "run_id": run.id,
            "status": run.status.value,
            "notes": run.notes,
            "context_gather": context_gather,
            "narrative_batches": dashboard_data.get("narrative_batches", []),
            "narrative_error": dashboard_data.get("narrative_error"),
        }
    finally:
        db.close()


@app.post("/tasks/daily-go-live-audit/run")
def trigger_daily_go_live_audit() -> dict:
    """Manual-trigger endpoint — same task the cron calls, run on demand.

    Returns a job_id immediately instead of blocking on the run (Bob,
    2026-08-11): a full run over the real account universe can take well
    past any client or Railway-proxy timeout. Poll
    GET .../run/{job_id} for the response body this endpoint used to return
    directly (see job_tracker.py — no task queue, just a background thread;
    this only ever runs once a day from one runtime, not concurrently)."""
    job_id = start_job(_run_daily_go_live_audit_and_summarize)
    return {"job_id": job_id, "job_status": "running"}


@app.get("/tasks/daily-go-live-audit/run/{job_id}")
def get_daily_go_live_audit_run_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if job["status"] == "running":
        return {"job_status": "running"}
    if job["status"] == "error":
        return {"job_status": "error", "error": job["error"]}
    return {"job_status": "done", **job["result"]}
