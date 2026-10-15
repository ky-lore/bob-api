"""
Sales audit dashboard routes (2026-10-15) -- "Pulse, but for sales data".

Same shape as routers/atlas_report.py: POST .../run kicks a background job
(job_tracker) and returns a job_id, GET .../run/{job_id} polls it, the
durable result is a SalesAuditRun row, and the HTML page always renders the
latest row (never job state), so it survives Railway redeploys.

Unlike Pulse the page itself is a data grid: the whole row set is embedded
as JSON and the sorting/filtering/search/rollups all run client-side (~130
rows -- instant, no round trips). The server's only jobs are persistence and
applying human overrides, which happens at render time (_apply_overrides) so a
correction survives every daily rebuild and shows immediately.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, get_session_factory
from app.integrations.atlas_client import AtlasClient, AtlasWriteError
from app.models import SalesAuditRun, SalesDealOverride
from app.routers.atlas_report import _ADMIN_OVERRIDE_PASSWORD, _require_admin_password
from app.tasks import sales_audit_config as cfg
from app.tasks.job_tracker import get_job, start_job
from app.tasks.sales_audit import run_and_store_sales_audit

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_DEAL_TYPES = {"Package", "Digital Diagnosis"}

# Brand images (2026-10-15), shipped in the repo rather than hotlinked from the marketing site so the dashboard
# can't break if that site's asset paths change. Whitelist of exact names -- never a path built from the request.
_BRAND_DIR = Path(__file__).resolve().parent.parent / "static" / "brand"
_BRAND_ASSETS = {"wordmark-black.png", "icon-mark-white.png", "favicon-64.png"}


class _OverrideRequest(BaseModel):
    deal_key: str
    client_name: str
    owner: str | None = None
    deal_type: str | None = None
    excluded: bool = False
    note: str | None = None
    set_by: str | None = None


def _apply_overrides(db: Session, rows: list[dict]) -> int:
    """Overlays current overrides onto a stored run's rows, in place. Returns
    how many rows carry one. Originals are kept under `auto_*` so the drawer
    can show what the pipeline said before the human changed it."""
    overrides = {o.deal_key: o for o in db.query(SalesDealOverride).all()}
    n = 0
    for r in rows:
        o = overrides.get(r["key"])
        r["overridden"] = bool(o)
        r["excluded"] = False
        r["override_note"] = None
        if not o:
            continue
        n += 1
        r["override_note"] = o.note
        r["excluded"] = bool(o.excluded)
        r["atlas_pushed_at"] = o.atlas_pushed_at.isoformat() if o.atlas_pushed_at else None
        if o.referral_partner:
            r["auto_source_type"] = r["source_type"]
            r["referral_partner"] = o.referral_partner
            r["source_type"], r["event"] = "Referral", None
            r["source_detail"] = (f"Referral partner: {o.referral_partner} "
                                  + ("(written to Atlas; shows there after the next refresh)" if o.atlas_pushed_at else "(set here only)"))
        if o.owner:
            r["auto_owner"], r["auto_owner_basis"] = r["owner"], r["owner_basis"]
            r["owner"], r["owner_basis"] = o.owner, "manual override"
            r["flags"] = [f for f in r["flags"] if f != "needs_rep"]
        if o.deal_type:
            r["auto_type"], r["type"] = r["type"], o.deal_type
    return n


def _roster_of(data: dict) -> list[dict]:
    """[{name, email}] for a stored run. Runs saved before the roster existed carry none -> the legacy four, which is
    only a bridge until the first run after the roster change."""
    return data.get("roster") or cfg.LEGACY_ROSTER


def _latest_roster(db: Session) -> list[dict]:
    run = db.query(SalesAuditRun).order_by(SalesAuditRun.run_at.desc()).first()
    return _roster_of(json.loads(run.report_json) if run else {})


def _load(db: Session, run: SalesAuditRun | None) -> dict:
    if run is None:
        return {"generated_at": None, "count": 0, "rows": [], "summary": {}, "source_errors": {}, "rules": {}}
    data = json.loads(run.report_json)
    data["roster"] = _roster_of(data)
    data["overrides_applied"] = _apply_overrides(db, data.get("rows", []))
    return data


def _page(request: Request, db: Session, run: SalesAuditRun | None) -> HTMLResponse:
    history = db.query(SalesAuditRun).order_by(SalesAuditRun.run_at.desc()).limit(30).all()
    return templates.TemplateResponse(request, "sales_audit.html", {
        "run": run, "history": history, "data": (data := _load(db, run)), "reps": [r["name"] for r in data.get("roster") or _roster_of({})],
        "needs_rep": cfg.NEEDS_REP, "admin_password": _ADMIN_OVERRIDE_PASSWORD,
        "atlas_web_base": get_settings().atlas_web_base_url.rstrip("/"),
    })


@router.get("/reports/sales-audit", response_class=HTMLResponse)
def latest_sales_audit(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    run = db.query(SalesAuditRun).order_by(SalesAuditRun.run_at.desc()).first()
    return _page(request, db, run)


@router.get("/reports/sales-audit/view/{run_id}", response_class=HTMLResponse)
def sales_audit_for_run(run_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    run = db.query(SalesAuditRun).filter_by(id=run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run_id")
    return _page(request, db, run)


@router.get("/reports/sales-audit/assets/{name}")
def brand_asset(name: str) -> FileResponse:
    if name not in _BRAND_ASSETS:
        raise HTTPException(status_code=404, detail="unknown asset")
    return FileResponse(_BRAND_DIR / name, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/reports/sales-audit/latest")
def latest_sales_audit_json(run_id: int | None = None, db: Session = Depends(get_db)) -> dict:
    """Latest run, or a specific one via ?run_id= (the page re-fetches this
    after an override save, including when viewing a historical run)."""
    q = db.query(SalesAuditRun)
    run = q.filter_by(id=run_id).first() if run_id is not None else q.order_by(SalesAuditRun.run_at.desc()).first()
    return {"run_id": run.id if run else None, "run_at": run.run_at.isoformat() if run else None, **_load(db, run)}


def _run_job(report_progress) -> dict:
    db = get_session_factory()()
    try:
        run = run_and_store_sales_audit(db, on_progress=report_progress)
        return {"run_id": run.id, "run_at": run.run_at.isoformat()}
    finally:
        db.close()


@router.post("/reports/sales-audit/run")
def trigger_sales_audit() -> dict:
    job_id = start_job(lambda report_progress: _run_job(report_progress))
    return {"job_id": job_id, "job_status": "running"}


@router.get("/reports/sales-audit/run/{job_id}")
def sales_audit_job_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if job["status"] == "running":
        return {"job_status": "running", "progress": job.get("progress")}
    if job["status"] == "error":
        return {"job_status": "error", "error": job["error"]}
    return {"job_status": "done", **job["result"]}


@router.post("/reports/sales-audit/overrides", dependencies=[Depends(_require_admin_password)])
def set_deal_override(payload: _OverrideRequest, db: Session = Depends(get_db)) -> dict:
    """Upserts the single override row for a deal. A request that sets nothing
    (no owner, no type, not excluded) clears the row instead of storing an
    empty one."""
    roster_names = [r["name"] for r in _latest_roster(db)]
    if payload.owner and payload.owner not in roster_names:
        raise HTTPException(status_code=422, detail=f"owner must be one of the Atlas sales roster: {roster_names}")
    if payload.deal_type and payload.deal_type not in _DEAL_TYPES:
        raise HTTPException(status_code=422, detail=f"deal_type must be one of {sorted(_DEAL_TYPES)}")

    existing = db.query(SalesDealOverride).filter_by(deal_key=payload.deal_key).first()
    if not (payload.owner or payload.deal_type or payload.excluded or (existing and existing.referral_partner)):
        if existing:
            db.delete(existing)
            db.commit()
        return {"ok": True, "cleared": bool(existing)}

    now = datetime.now(timezone.utc)
    if existing is None:
        existing = SalesDealOverride(deal_key=payload.deal_key, created_at=now)
        db.add(existing)
    existing.client_name = payload.client_name
    existing.owner = payload.owner
    existing.deal_type = payload.deal_type
    existing.excluded = payload.excluded
    existing.note = payload.note
    existing.set_by = payload.set_by
    existing.updated_at = now
    db.commit()
    return {"ok": True}


class _PushRequest(BaseModel):
    deal_key: str
    owner: str | None = None             # one of the four core rep names
    referral_partner: str | None = None  # a partner name (Atlas find-or-creates it)
    set_by: str
    # Audit-only fields from the same form (never sent to Atlas); applied to the local override if given.
    deal_type: str | None = None
    excluded: bool | None = None
    note: str | None = None


def _atlas_client() -> AtlasClient:
    return AtlasClient()


@router.post("/reports/sales-audit/push", dependencies=[Depends(_require_admin_password)])
def push_to_atlas(payload: _PushRequest, db: Session = Depends(get_db)) -> dict:
    """Writes a deal's rep and/or referral partner to ATLAS (the source of
    truth) and mirrors it locally as an override (2026-10-15; Bob: "anyone with
    access to it will have access to write -- we can restrict later").

    The local override is saved FIRST, so the person's intent is never lost if
    Atlas rejects or is unreachable: the response is then `pushed: false` with
    Atlas's reason, and the override simply stays local. On success it is
    stamped atlas_pushed_at, and retired automatically by the next daily run
    once Atlas's own value shows through (sales_audit.reconcile_overrides).

    Only deals that have an Atlas account can be pushed (422 otherwise). The
    deal -> Atlas id mapping comes from the stored run, never from the client."""
    owner = payload.owner
    partner = payload.referral_partner.strip() if payload.referral_partner is not None else None
    if owner is None and partner is None:
        raise HTTPException(status_code=422, detail="provide owner and/or referral_partner")
    roster = {r["name"]: r["email"] for r in _latest_roster(db)}
    if owner is not None and owner not in roster:
        raise HTTPException(status_code=422, detail=f"owner must be one of the Atlas sales roster: {list(roster)}")
    if partner is not None and not partner:
        raise HTTPException(status_code=422, detail="referral_partner cannot be blank")
    if payload.deal_type and payload.deal_type not in _DEAL_TYPES:
        raise HTTPException(status_code=422, detail=f"deal_type must be one of {sorted(_DEAL_TYPES)}")

    run = db.query(SalesAuditRun).order_by(SalesAuditRun.run_at.desc()).first()
    row = next((r for r in json.loads(run.report_json).get("rows", []) if r["key"] == payload.deal_key), None) if run else None
    if row is None:
        raise HTTPException(status_code=404, detail="unknown deal_key")
    if not row.get("atlas_id"):
        raise HTTPException(status_code=422, detail="this deal has no Atlas account to update")

    now = datetime.now(timezone.utc)
    existing = db.query(SalesDealOverride).filter_by(deal_key=payload.deal_key).first()
    if existing is None:
        existing = SalesDealOverride(deal_key=payload.deal_key, created_at=now)
        db.add(existing)
    existing.client_name = row["client"]
    if owner is not None:
        existing.owner = owner
    if partner is not None:
        existing.referral_partner = partner
    if payload.deal_type is not None:
        existing.deal_type = payload.deal_type or None
    if payload.excluded is not None:
        existing.excluded = payload.excluded
    if payload.note is not None:
        existing.note = payload.note or None
    existing.set_by = payload.set_by
    existing.atlas_pushed_at = None
    existing.updated_at = now
    db.commit()

    kwargs = {}
    if owner is not None:
        kwargs["sales_rep"] = roster[owner]
    if partner is not None:
        kwargs["referral_partner"] = partner
    try:
        result = _atlas_client().set_sales_attribution(row["atlas_id"], actor=payload.set_by, **kwargs)
    except AtlasWriteError as exc:
        hint = " (the Atlas endpoint may not be deployed yet)" if exc.status == 404 else ""
        return {"ok": True, "pushed": False, "error": exc.message + hint, "saved_locally": True}
    except Exception as exc:  # network/timeouts: still saved locally
        return {"ok": True, "pushed": False, "error": f"{type(exc).__name__}: {exc}"[:300], "saved_locally": True}

    existing.atlas_pushed_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "pushed": True, "changed": result.get("changed", []), "atlas": {
        "salesRep": result.get("salesRep"), "referralPartner": result.get("referralPartner")}}


@router.delete("/reports/sales-audit/overrides/{deal_key:path}", dependencies=[Depends(_require_admin_password)])
def clear_deal_override(deal_key: str, db: Session = Depends(get_db)) -> dict:
    deleted = db.query(SalesDealOverride).filter_by(deal_key=deal_key).delete()
    db.commit()
    return {"ok": True, "cleared": bool(deleted)}
