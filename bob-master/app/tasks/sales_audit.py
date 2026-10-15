"""
Sales audit orchestrator (2026-10-15): pulls every source, runs the blend
(sales_audit_blend.py), and persists one SalesAuditRun per execution -- the
"Pulse for sales data". Productionizes the one-off Jun-Oct 2026 audit
(handoff BRIEF.md in the repo's sales-audit-handoff.zip).

Failure posture, deliberately split in two:
- Atlas and GHL opps are the spine. If either fails the run RAISES and
  stores nothing -- half a universe silently showing as "the" sales list is
  worse than the page keeping yesterday's good run and its staleness showing.
- The billing sheet and Zoom are supporting evidence. If either fails the
  run still completes and records the failure under `source_errors`, which
  the page shows as a banner (amounts fall back to Atlas billing/notes; rep
  attribution loses its call-evidence tier).

LLM synthesis (exec narrative, rep-suggestions-from-transcripts) is
deliberately NOT in this phase -- every number here is deterministic Python,
reproducible and explainable. See the build notes in chat, 2026-10-15.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.integrations.atlas_client import AtlasClient
from app.integrations.ghl import GHLClient
from app.integrations.google_drive import GoogleDriveClient
from app.models import SalesAuditRun, SalesDealOverride
from app.tasks import sales_audit_config as cfg
from app.tasks.sales_audit_blend import DIGITAL_DIAGNOSIS, PACKAGE, Roster, build_sales_rows
from app.tasks.sales_audit_sources import ZoomIndex, fetch_ghl, make_lookups, read_billing

logger = logging.getLogger(__name__)


def _summary(rows: list[dict]) -> dict:
    def tally(kind: str) -> dict:
        subset = [r for r in rows if r["type"] == kind]
        return {"count": len(subset), "amount": sum(r["amount"] or 0 for r in subset),
                "needs_rep": sum(1 for r in subset if r["owner"] == cfg.NEEDS_REP)}
    return {"package": tally(PACKAGE), "digital_diagnosis": tally(DIGITAL_DIAGNOSIS),
            "flags": dict(Counter(f for r in rows for f in r["flags"]))}


def _atlas_roster(atlas, db: Session, ghl_user_details: list[dict], source_errors: dict) -> Roster:
    """The rep roster = Atlas's GET /api/users/sales-reps, every run (Bob: the endpoint is the level of truth).
    If it can't be reached -- or returns nobody, which would silently credit no one -- fall back to the last
    stored run's roster, then LEGACY_ROSTER, and SAY SO on the page (source_errors) rather than guessing quietly."""
    try:
        reps = atlas.get_sales_reps()
        if reps:
            return Roster.from_atlas(reps, ghl_user_details)
        source_errors["atlas_roster"] = "Atlas returned an empty sales roster; using the last known roster"
    except Exception as exc:
        logger.exception("sales-audit: sales-roster fetch failed")
        source_errors["atlas_roster"] = f"{type(exc).__name__}: {exc}"[:300] + " -- using the last known roster"
    previous = db.query(SalesAuditRun).order_by(SalesAuditRun.run_at.desc()).first()
    stored = json.loads(previous.report_json).get("roster") if previous else None
    return Roster.from_atlas(stored or cfg.LEGACY_ROSTER, ghl_user_details)


def build_sales_audit(
    db: Session, *, atlas=None, ghl=None, drive=None, zoom: ZoomIndex | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Clients are injectable for tests; defaults build the real ones."""
    note = on_progress or (lambda _msg: None)
    source_errors: dict[str, str] = {}

    note("pulling Atlas accounts")
    atlas = atlas or AtlasClient()
    atlas_accounts = atlas.get_all_accounts()

    note("pulling GHL opportunities")
    ghl = ghl or GHLClient()
    ghl_data = fetch_ghl(ghl)

    note("pulling the Atlas sales roster")
    roster = _atlas_roster(atlas, db, ghl_data.get("ghl_user_details", []), source_errors)

    note("reading billing sheet")
    billing: list[dict] = []
    billing_meta: dict = {}
    try:
        billing, billing_meta = read_billing(drive or GoogleDriveClient())
    except Exception as exc:  # soft: amounts fall back to Atlas billing/notes
        logger.exception("sales-audit: billing sheet read failed")
        source_errors["billing_sheet"] = f"{type(exc).__name__}: {exc}"[:300]

    note("indexing Zoom transcripts")
    try:
        zoom = zoom or ZoomIndex.from_db(db, roster)
    except Exception as exc:  # soft: rep attribution loses call evidence
        logger.exception("sales-audit: zoom index failed")
        source_errors["zoom"] = f"{type(exc).__name__}: {exc}"[:300]
        zoom = ZoomIndex([])

    note("blending")
    rows = build_sales_rows({"atlas": atlas_accounts, "billing": billing, "roster": roster, **ghl_data}, make_lookups(ghl, zoom))

    # first_seen carry-forward: GHL won dates are unreliable, so remember the
    # first day each deal appeared in ANY audit snapshot.
    today = datetime.now(timezone.utc).date().isoformat()
    previous = db.query(SalesAuditRun).order_by(SalesAuditRun.run_at.desc()).first()
    seen = {r["key"]: r.get("first_seen") for r in json.loads(previous.report_json).get("rows", [])} if previous else {}
    for r in rows:
        r["first_seen"] = seen.get(r["key"]) or today

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows), "rows": rows, "summary": _summary(rows),
        "source_errors": source_errors, "billing_meta": billing_meta,
        "zoom_transcripts_indexed": len(zoom.entries),
        "roster": roster.reps,
        "rules": {"audit_start": cfg.AUDIT_START, "atlas_era_start": cfg.ATLAS_ERA_START, "reps": roster.names},
    }


def reconcile_overrides(db: Session, rows: list[dict]) -> int:
    """Retires the parts of a local override that Atlas now says itself
    (2026-10-15). A rep/referral partner pushed to Atlas stays as a local
    override only until a run shows it coming back from Atlas -- after that
    Atlas is the single record and the local copy is redundant (and would
    mask a later Atlas edit). Only fields that AGREE are cleared; anything
    still differing from Atlas stays. type/excluded overrides are audit-only
    judgments (never in Atlas) and are untouched. Returns overrides changed."""
    by_key = {r["key"]: r for r in rows}
    changed = 0
    for o in db.query(SalesDealOverride).all():
        row = by_key.get(o.deal_key)
        if row is None:
            continue
        touched = False
        if o.owner and row["owner"] == o.owner and row["owner_basis"] == "Atlas salesRep":
            o.owner, touched = None, True
        if (o.referral_partner and row.get("referral_partner") and row["referral_partner"].lower() == o.referral_partner.lower()
                and str(row.get("source_detail") or "").startswith("Atlas referral partner")):
            o.referral_partner, touched = None, True
        if not touched:
            continue
        changed += 1
        if not o.owner and not o.referral_partner:
            o.atlas_pushed_at = None
        if not (o.owner or o.referral_partner or o.deal_type or o.excluded or o.note):
            db.delete(o)
    if changed:
        db.commit()
    return changed


def run_and_store_sales_audit(db: Session, *, on_progress: Callable[[str], None] | None = None, **clients) -> SalesAuditRun:
    payload = build_sales_audit(db, on_progress=on_progress, **clients)
    run = SalesAuditRun(run_at=datetime.now(timezone.utc).replace(tzinfo=None), report_json=json.dumps(payload))
    db.add(run)
    db.commit()
    db.refresh(run)
    try:
        retired = reconcile_overrides(db, payload["rows"])
        if retired:
            logger.info("sales-audit: retired %d local override(s) that Atlas now agrees with", retired)
    except Exception:
        logger.exception("sales-audit: override reconcile failed (run itself is stored)")
        db.rollback()
    logger.info("sales-audit: run %s stored (%d rows, errors=%s)", run.id, payload["count"], list(payload["source_errors"]))
    return run
