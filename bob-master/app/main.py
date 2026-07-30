from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import Session
from fastapi import Depends

from app.db import get_db, init_db
from app.routers import admin, dashboard
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/tasks/daily-go-live-audit/run")
def trigger_daily_go_live_audit(db: Session = Depends(get_db)) -> dict:
    """Manual-trigger endpoint — same task the cron calls, run on demand."""
    run = run_daily_go_live_audit(db)
    return {"run_id": run.id, "status": run.status.value}
