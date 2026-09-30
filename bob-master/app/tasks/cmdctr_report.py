"""
Atlas Command Center's fast-refresh pipeline (2026-09-29) -- deliberately
NOT a copy of app/tasks/atlas_report.py's weekly full-universe Pulse run,
just a differently-configured call into the exact same machinery
(gather_atlas_context, synthesize_account_reports): scoped to ONLY the
pipeline-stage (onboarding/development) account set -- Atlas's own
"signing to go-live" fulfillment queue, typically 5-10 accounts -- and
refreshed hourly instead of weekly.

The reason Pulse can't run hourly is account count (~150), which forces
tight batching/a conservative token cap to avoid re-triggering the
stop_reason=max_tokens incident anthropic_client.py's module docstring
warns about. At 5-10 accounts there's no such pressure, so this run
loosens both restrictions instead: one single batch (no cross-account
context lost at a batch boundary) with a much higher per-account token
ceiling. It ALSO swaps in a genuinely deeper prompt/schema
(_CMDCTR_REPORT_SYSTEM_PROMPT/_CMDCTR_REPORT_TOOL_SCHEMA in
anthropic_client.py) -- confirmed the hard way, 2026-09-29 (Chris:
"doesn't look that much more granular/expanded"): raising the token
BUDGET alone did nothing, since the shared prompt still explicitly asked
for one concise sentence regardless of how much room there was to use.

Also re-syncs the ClickUp "admin"-named action-item relay
(app/tasks/standup_action_items.py) on every run -- Chris: "it should
also still be polling the CU for the new 'admin' string." Bundled here
(not just on the scheduler's hourly tick) so a manual trigger via
POST /reports/cmdctr/run refreshes both the LLM blend AND the action-item
list identically to the scheduled run, not just one of them.

Does NOT touch AtlasReportRun or the main /pulse dashboard's read path at
all -- persists its own CmdctrRun row instead (see app/models.py) so a
narrow, high-frequency run can never become what GET .../latest or
.../pulse renders for the full-universe exec dashboard.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.anthropic_client import _CMDCTR_REPORT_SYSTEM_PROMPT, _CMDCTR_REPORT_TOOL_SCHEMA
from app.integrations.atlas_client import AtlasClient
from app.models import CmdctrRun
from app.tasks.atlas_report import _is_pipeline_stage, _pulse_run_payload, build_atlas_report
from app.tasks.standup_action_items import sync_standup_action_items

logger = logging.getLogger(__name__)

# With only ~5-10 accounts total, one single batch call (a ceiling high
# enough that len(accounts) never actually splits) beats several small ones
# -- lets the model reason across the whole pipeline queue in one pass.
_CMDCTR_BATCH_SIZE = 25
# 4x anthropic_client._MAX_TOKENS_CAP -- same per-account multiplier the
# weekly run uses, just no longer squeezed by a budget sized for a
# 5-account BATCH out of ~30, rather than a 5-10 account TOTAL RUN.
_CMDCTR_MAX_TOKENS_CAP = 16384


def build_cmdctr_report(
    db: Session | None = None, on_progress=None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Same shape/contract as build_atlas_report -- see its docstring --
    just pre-filtered to _is_pipeline_stage accounts, with the batching/
    token limits loosened, and the deep-dive prompt/schema swapped in
    (see module docstring)."""
    return build_atlas_report(
        db=db,
        on_progress=on_progress,
        accounts_filter=_is_pipeline_stage,
        batch_size=_CMDCTR_BATCH_SIZE,
        max_tokens_cap=_CMDCTR_MAX_TOKENS_CAP,
        system_prompt=_CMDCTR_REPORT_SYSTEM_PROMPT,
        tool_schema=_CMDCTR_REPORT_TOOL_SCHEMA,
    )


def _sync_admin_action_items() -> dict[str, Any]:
    """Soft-failed and logged, never allowed to break the CMDCTR run
    itself -- same posture as the pulse_push try/except below. Returns
    sync_standup_action_items's own result dict on success, or
    {"ok": False, "error": str} on failure, both stored onto the run so
    the outcome is readable via GET .../latest instead of silently
    vanishing (same reasoning as pulse_push -- see run_and_store_atlas_report)."""
    try:
        result = sync_standup_action_items()
        logger.info("cmdctr: admin action-item sync ok: %s", result)
        return {"ok": True, **result}
    except Exception as exc:
        logger.exception("cmdctr: admin action-item sync failed")
        return {"ok": False, "error": str(exc)}


def run_and_push_cmdctr_report(db: Session, on_progress=None) -> CmdctrRun:
    """Runs build_cmdctr_report, persists it as a new CmdctrRun row (Bob's
    own lightweight diagnostic trail -- never AtlasReportRun, see module
    docstring), pushes it to Atlas's Command Center, and re-syncs the
    ClickUp "admin" action-item relay (see module docstring for why this
    is bundled here). Reuses _pulse_run_payload as-is (duck-types on
    run.id/run.run_at, which CmdctrRun carries under the same field names
    as AtlasReportRun) -- same soft-fail posture as
    run_and_store_atlas_report: neither an Atlas push failure nor an
    admin-sync failure can lose Bob's own already-computed result, and
    both real outcomes are recorded back onto the row rather than
    swallowed."""
    records, batch_results = build_cmdctr_report(db=db, on_progress=on_progress)
    run = CmdctrRun(
        run_at=datetime.now(timezone.utc),
        report_json=json.dumps({"count": len(records), "accounts": records, "narrative_batches": batch_results}),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    logger.info("cmdctr: run %s persisted (%d accounts)", run.id, len(records))

    push_ok = True
    push_error: str | None = None
    try:
        AtlasClient().post_pulse_run(_pulse_run_payload(run, records))
        logger.info("cmdctr: run %s pushed to Atlas Command Center", run.id)
    except Exception as exc:
        push_ok = False
        push_error = str(exc)
        logger.exception("cmdctr: run %s Command Center push failed", run.id)

    admin_sync_result = _sync_admin_action_items()

    data = json.loads(run.report_json)
    data["pulse_push"] = {"ok": push_ok, "error": push_error}
    data["admin_sync"] = admin_sync_result
    run.report_json = json.dumps(data)
    db.commit()
    db.refresh(run)
    return run
