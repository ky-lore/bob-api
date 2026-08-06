from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import Session
from fastapi import Depends

from adspend.main import app as adspend_app
from app.db import get_db, init_db
from app.routers import admin, atlas_report, dashboard
from app.scheduler import start_scheduler
from app.tasks.daily_go_live_audit import run_daily_go_live_audit


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


@app.post("/tasks/daily-go-live-audit/run")
def trigger_daily_go_live_audit(db: Session = Depends(get_db)) -> dict:
    """Manual-trigger endpoint — same task the cron calls, run on demand.

    Response body includes the MVP rich-context gather diagnostics and Claude
    narrative-batch outcomes (per Bob, 2026-07-31) — not just run_id/status —
    since this pipeline (account_context_gather.py, full ClickUp/Slack pulls)
    is new and unproven at real volume; seeing per-account/per-batch
    success-failure here beats digging through run.notes or logs."""
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
