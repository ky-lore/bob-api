"""
In-memory status tracking for background jobs kicked off by a manual-trigger
HTTP endpoint -- e.g. the daily-go-live-audit trigger, and any future daily
task that ends up needing the same treatment (see chat, 2026-08-11: an
LLM-blended daily pipeline is triggered once a day from one runtime, not by
many concurrent users -- a real task queue, Celery/RQ plus Redis plus a
separate worker service, solves a problem this app doesn't have).

This is a job-status board, not a queue: it doesn't persist across a
restart or coordinate across processes, which is fine given there's a
single Railway web process. The actual result of a real run still lands in
that task's own DB row (e.g. AuditRun) -- this only answers "is job X still
going, and what happened" for whoever triggered it.
"""
from __future__ import annotations

import inspect
import threading
import uuid
from typing import Any, Callable

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def start_job(fn: Callable[[], dict[str, Any]] | Callable[[Callable[[Any], None]], dict[str, Any]]) -> str:
    """Runs fn() on a background thread; returns a job_id immediately for
    get_job() polling. fn must open/close its own DB session -- the
    request's session is gone by the time this runs.

    Progress (2026-09-18): if fn accepts one parameter, it's called as
    fn(report_progress) instead of fn() -- report_progress(x) stashes
    whatever x is (a plain dict is the convention every caller uses so far,
    e.g. {"phase": ..., "completed": ..., "total": ...}) as this job's
    "progress", visible to a poller via get_job() while status is still
    "running". Zero-arg callers (every job before this one) are unaffected --
    this was added because a real ~11-minute unlimited atlas-report run was a
    total black box while running, with no way to tell it apart from a hung
    process short of waiting it out."""
    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {"status": "running", "result": None, "error": None, "progress": None}

    wants_progress = len(inspect.signature(fn).parameters) > 0

    def _report_progress(progress: Any) -> None:
        with _lock:
            if _jobs.get(job_id, {}).get("status") == "running":
                _jobs[job_id]["progress"] = progress

    def _run() -> None:
        try:
            result = fn(_report_progress) if wants_progress else fn()
            with _lock:
                _jobs[job_id] = {"status": "done", "result": result, "error": None, "progress": None}
        except Exception as exc:
            with _lock:
                _jobs[job_id] = {"status": "error", "result": None, "error": str(exc), "progress": None}

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        return _jobs.get(job_id)
