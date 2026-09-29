"""
Manual-trigger endpoint for Command Center's fast-refresh pipeline -- see
app/tasks/cmdctr_report.py. Manual-trigger only for now, not wired into the
scheduler: same rollout posture as every other new external push in this
app (review a real run's output before anything runs unattended) -- in
particular, this needs a real run confirmed against Atlas's own Command
Center UI before an hourly cron goes live, since it's unconfirmed from
Bob's side alone whether Atlas's POST /api/pulse-runs upserts per-account
or replaces the whole stored picture; the latter would mean an hourly
5-10-account push blanks out every other account between full weekly
Pulse runs. Same job_id/poll shape as every other background task here.
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db, get_session_factory
from app.models import CmdctrRun
from app.tasks.cmdctr_report import run_and_push_cmdctr_report
from app.tasks.job_tracker import get_job, start_job

router = APIRouter()


def _run_to_response(run: CmdctrRun) -> dict:
    data = json.loads(run.report_json)
    return {"run_id": run.id, "run_at": run.run_at.isoformat(), **data}


def _run_cmdctr_job(report_progress) -> dict:
    db = get_session_factory()()
    try:
        run = run_and_push_cmdctr_report(db, on_progress=report_progress)
        return _run_to_response(run)
    finally:
        db.close()


@router.post("/reports/cmdctr/run")
def trigger_cmdctr_report() -> dict:
    job_id = start_job(lambda report_progress: _run_cmdctr_job(report_progress))
    return {"job_id": job_id, "job_status": "running"}


@router.get("/reports/cmdctr/run/{job_id}")
def get_cmdctr_report_run_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if job["status"] == "running":
        return {"job_status": "running", "progress": job.get("progress")}
    if job["status"] == "error":
        return {"job_status": "error", "error": job["error"]}
    return {"job_status": "done", **job["result"]}


@router.get("/reports/cmdctr/latest")
def get_latest_cmdctr_report(db: Session = Depends(get_db)) -> dict:
    run = db.query(CmdctrRun).order_by(CmdctrRun.run_at.desc()).first()
    if run is None:
        return {"run_id": None, "run_at": None, "count": 0, "accounts": [], "narrative_batches": []}
    return _run_to_response(run)
