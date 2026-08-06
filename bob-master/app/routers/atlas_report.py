"""
GET endpoint for Atlas to pull the consolidated per-account report (see
app/tasks/atlas_report.py) — presumably via a daily cron on Atlas's side.
Recomputes live on every call; no caching layer, per Bob (2026-08-06):
"inefficiency isn't a big deal, it'll run on a daily basis."
"""
from fastapi import APIRouter, Query

from app.tasks.atlas_report import build_atlas_report

router = APIRouter()


@router.get("/reports/atlas-account-status")
def get_atlas_account_status_report(
    limit: int | None = Query(default=None, description="Cap the account universe (smoke-testing only)"),
) -> dict:
    records, narrative_batches = build_atlas_report(limit=limit)
    return {"count": len(records), "accounts": records, "narrative_batches": narrative_batches}
