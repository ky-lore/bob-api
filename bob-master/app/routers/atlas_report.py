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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db, get_session_factory
from app.integrations.anthropic_client import _HEALTH_VALUES
from app.models import AccountHealthOverride, AtlasReportRun
from app.tasks.atlas_report import build_atlas_report, run_and_store_atlas_report
from app.tasks.job_tracker import get_job, start_job

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_HEALTH_ORDER = {"at_risk": 0, "needs_attention": 1, "on_track": 2}
_HEALTH_LABEL = {"at_risk": "At risk", "needs_attention": "Needs attention", "on_track": "On track"}

# Pulse's "Not live yet" section membership is Atlas's own STAGE, not the
# is_live boolean (2026-09-21, Bob: "Remember, onboarding and development
# only") -- is_live is stage=="live" specifically (see build_atlas_report),
# which meant an At Risk or Closed stage account (neither of which is the
# literal string "live") was falling into "not live" right alongside a
# genuine pre-launch account, even though neither is actually pre-launch.
# Closed is excluded from Pulse ENTIRELY (Bob: "completely ignored for this
# purpose") -- it's neither pipeline nor an active client, showing it in
# either section is just noise. At Risk is deliberately NOT pipeline: by
# elimination it lands in the Live section below, since an at-risk account
# is presumably a currently-or-recently-live client flagged for churn risk,
# not a pre-launch prospect.
_PIPELINE_STAGES = {"onboarding", "development"}
_EXCLUDED_STAGES = {"closed"}


def _account_stage(a: dict) -> str:
    return (a.get("stage") or "").lower()


def _is_pipeline_stage(a: dict) -> bool:
    return _account_stage(a) in _PIPELINE_STAGES


def _is_excluded_stage(a: dict) -> bool:
    return _account_stage(a) in _EXCLUDED_STAGES


# Hardcoded, not a Settings/env var (2026-09-21, Bob: "forget .env - can we
# just store it static on-page?") -- this is a rudimentary internal-only
# gate, not real auth (see AccountHealthOverride's docstring), and Bob
# explicitly wants zero deployment/config step to use it. Must match the
# ADMIN_PASSWORD constant in account_pulse.html's script exactly.
_ADMIN_OVERRIDE_PASSWORD = "wasp"


def _require_admin_password(x_admin_password: str | None = Header(default=None)) -> None:
    if x_admin_password != _ADMIN_OVERRIDE_PASSWORD:
        raise HTTPException(status_code=401, detail="invalid or missing admin password")


class _OverrideRequest(BaseModel):
    atlas_id: str
    company_name: str
    health: str
    reason: str | None = None
    set_by: str | None = None


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


def _run_atlas_report_job(limit: int | None, report_progress) -> dict:
    """Runs on job_tracker's background thread -- needs its own DB session
    since the request's is long gone by the time this executes (same
    reasoning as main.py's _run_daily_go_live_audit_and_summarize).
    report_progress: job_tracker's callback (see start_job) -- threaded
    straight through to run_and_store_atlas_report's on_progress."""
    db = get_session_factory()()
    try:
        run = run_and_store_atlas_report(db, limit=limit, on_progress=report_progress)
        return _run_to_response(run)
    finally:
        db.close()


@router.post("/reports/atlas-account-status/run")
def trigger_atlas_report(
    limit: int | None = Query(default=None, description="Cap the account universe (smoke-testing only)"),
) -> dict:
    """Manual-trigger endpoint for the full report, run in the background.
    Returns a job_id immediately instead of blocking (see module docstring
    for why). Poll GET .../run/{job_id} for status (now including a
    "progress" field, 2026-09-18 -- a real unlimited run takes ~11 minutes
    and was otherwise a total black box while running), or GET .../latest
    once it's done -- the latter survives a redeploy mid-run, the former
    doesn't."""
    job_id = start_job(lambda report_progress: _run_atlas_report_job(limit, report_progress))
    return {"job_id": job_id, "job_status": "running"}


@router.get("/reports/atlas-account-status/run/{job_id}")
def get_atlas_report_run_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if job["status"] == "running":
        return {"job_status": "running", "progress": job.get("progress")}
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


@router.post("/reports/atlas-account-status/overrides", dependencies=[Depends(_require_admin_password)])
def set_health_override(payload: _OverrideRequest, db: Session = Depends(get_db)) -> dict:
    """Sets (or replaces) the single override row for this account -- see
    AccountHealthOverride's docstring for why there's no history. Applied
    immediately on the NEXT read of /pulse (see _apply_live_overrides), not
    just the next full run -- a correction shouldn't need an ~11-minute
    re-run over the whole account universe to take effect."""
    if payload.health not in _HEALTH_VALUES:
        raise HTTPException(status_code=422, detail=f"health must be one of {sorted(_HEALTH_VALUES)}")

    now = datetime.now(timezone.utc)
    existing = db.query(AccountHealthOverride).filter_by(atlas_id=payload.atlas_id).first()
    if existing:
        existing.company_name = payload.company_name
        existing.health = payload.health
        existing.reason = payload.reason
        existing.set_by = payload.set_by
        existing.updated_at = now
    else:
        db.add(AccountHealthOverride(
            atlas_id=payload.atlas_id,
            company_name=payload.company_name,
            health=payload.health,
            reason=payload.reason,
            set_by=payload.set_by,
            created_at=now,
            updated_at=now,
        ))
    db.commit()
    return {"ok": True}


@router.delete("/reports/atlas-account-status/overrides/{atlas_id}", dependencies=[Depends(_require_admin_password)])
def clear_health_override(atlas_id: str, db: Session = Depends(get_db)) -> dict:
    deleted = db.query(AccountHealthOverride).filter_by(atlas_id=atlas_id).delete()
    db.commit()
    return {"ok": True, "cleared": bool(deleted)}


def _apply_live_overrides(db: Session, accounts: list[dict]) -> None:
    """Reapplies whatever overrides exist RIGHT NOW onto an already-persisted
    run's account list, in place -- so setting/clearing an override shows up
    on /pulse immediately, even for a run that was generated before the
    override existed (see set_health_override's docstring). build_atlas_report
    bakes overrides in too (for API consumers that only ever hit the plain
    JSON endpoints), but that copy goes stale the moment someone changes an
    override without triggering a brand new run -- this is what keeps /pulse
    itself always current regardless of that staleness."""
    overrides = {o.atlas_id: o for o in db.query(AccountHealthOverride).all()}
    for a in accounts:
        override = overrides.get(a.get("atlas_id"))
        if override:
            a.setdefault("llm_health", a.get("health"))
            a["health"] = override.health
            a["health_overridden"] = True
            a["health_override_reason"] = override.reason
        else:
            a["health_overridden"] = False
            a["health_override_reason"] = None
            a.pop("llm_health", None)


def _needs_extra_focus(a: dict) -> bool:
    """A genuine pipeline account (Onboarding/Development stage -- see
    _PIPELINE_STAGES) that's already flagged (2026-09-21, Bob: "these are
    the ones we typically want to focus on a bit more than active clients")
    -- a live client having a rough week is being watched by the regular ad
    pipeline regardless; a pre-launch account that's ALSO flagged risks
    losing the client before they ever go live, which is a different,
    higher-priority kind of problem. Stage-based, not is_live-based (fixed
    2026-09-21 alongside the section split below) -- is_live only being
    stage=="live" meant this used to also fire for At Risk/Closed-stage
    accounts, neither of which is actually pre-launch."""
    return _is_pipeline_stage(a) and a.get("health") in ("at_risk", "needs_attention")


def _display_ready(a: dict) -> dict:
    """Precomputes every string a Jinja template needs so the template stays
    pure presentation -- same reasoning as keeping business logic out of
    dashboard.html's Jinja (see dashboard_summary.py). Mirrors the one-off
    Artifact preview built 2026-09-18 for the 15-account sample, now the
    real server-rendered view Bob asked for after seeing that preview."""
    stage = (a.get("stage") or "unknown").lower()
    spend = a.get("ad_spend")
    bits = []
    if a.get("google_ads"):
        bits.append(f"Google ${a['google_ads']['total_cost']:,.0f}")
    elif a.get("google_ads_error"):
        bits.append("Google: pull failed")
    if a.get("meta_ads"):
        bits.append(f"Meta ${a['meta_ads']['total_cost']:,.0f}")
    elif a.get("meta_ads_error"):
        bits.append("Meta: pull failed")

    return {
        **a,
        "health_label": _HEALTH_LABEL.get(a.get("health"), a.get("health")),
        "llm_health_label": _HEALTH_LABEL.get(a.get("llm_health"), a.get("llm_health")),
        "needs_extra_focus": _needs_extra_focus(a),
        "stage_bit": None if stage in ("live", "unknown") else stage.title(),
        "has_spend": spend is not None,
        "spend_total_display": f"${spend['total_spend']:,.0f}" if spend else None,
        "spend_conversions_display": f"{spend['total_conversions']:g}" if spend else None,
        "spend_cpc_display": (
            f"${spend['cost_per_conversion']:,.2f}" if spend and spend.get("cost_per_conversion") is not None else "—"
        ),
        "platform_line": " · ".join(bits) if bits else "No ad platform on file",
    }


def _empty_health_counts() -> dict:
    return {"at_risk": 0, "needs_attention": 0, "on_track": 0}


def _count_health(accounts: list[dict]) -> dict:
    counts = _empty_health_counts()
    for a in accounts:
        key = a.get("health") if a.get("health") in counts else "on_track"
        counts[key] += 1
    return counts


def _pulse_context(db: Session, run: AtlasReportRun | None) -> dict:
    """Split into two sections, not one merged/sorted list (2026-09-21, Bob:
    "go-live/non-live accounts are honestly treated in their complete other
    scope... execs want to focus on those heavily") -- a live account's rough
    week and a not-yet-live account's rough week are different categories of
    problem to an exec, not just different severities of the same one.
    Stacked sections (not tabs, not side-by-side columns) so nothing is ever
    hidden behind a click and cards keep full width for the status/recent-work
    prose -- see chat, this was a deliberate call against both alternatives."""
    history = db.query(AtlasReportRun).order_by(AtlasReportRun.run_at.desc()).limit(30).all()
    pipeline_accounts: list[dict] = []
    live_accounts: list[dict] = []
    health_counts = _empty_health_counts()
    pipeline_health_counts = _empty_health_counts()
    live_health_counts = _empty_health_counts()
    if run is not None:
        data = json.loads(run.report_json)
        raw_accounts = [a for a in data.get("accounts", []) if not _is_excluded_stage(a)]
        # Live overrides applied (and can re-sort/re-bucket an account) BEFORE
        # sorting/counting -- see _apply_live_overrides's docstring.
        _apply_live_overrides(db, raw_accounts)
        raw_accounts.sort(
            key=lambda a: (_HEALTH_ORDER.get(a.get("health"), 3), 0 if _needs_extra_focus(a) else 1, -(a.get("day") or 0))
        )
        # Splitting a stably-sorted list preserves each group's own
        # severity/day ordering -- no need to sort twice. Live section is
        # "everything that isn't pipeline" (by elimination), not literally
        # is_live -- an At Risk-stage account belongs here too, see
        # _PIPELINE_STAGES's docstring.
        raw_pipeline = [a for a in raw_accounts if _is_pipeline_stage(a)]
        raw_live = [a for a in raw_accounts if not _is_pipeline_stage(a)]
        pipeline_accounts = [_display_ready(a) for a in raw_pipeline]
        live_accounts = [_display_ready(a) for a in raw_live]
        health_counts = _count_health(raw_accounts)
        pipeline_health_counts = _count_health(raw_pipeline)
        live_health_counts = _count_health(raw_live)
    return {
        "run": run,
        "history": history,
        "health_counts": health_counts,
        "pipeline_accounts": pipeline_accounts,
        "pipeline_health_counts": pipeline_health_counts,
        "live_accounts": live_accounts,
        "live_health_counts": live_health_counts,
    }


@router.get("/reports/atlas-account-status/pulse", response_class=HTMLResponse)
def latest_atlas_report_pulse(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    run = db.query(AtlasReportRun).order_by(AtlasReportRun.run_at.desc()).first()
    return templates.TemplateResponse(request, "account_pulse.html", _pulse_context(db, run))


@router.get("/reports/atlas-account-status/pulse/{run_id}", response_class=HTMLResponse)
def atlas_report_pulse_for_run(run_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    run = db.query(AtlasReportRun).filter_by(id=run_id).first()
    return templates.TemplateResponse(request, "account_pulse.html", _pulse_context(db, run))
