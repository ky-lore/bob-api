import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.db import get_session_factory
from app.tasks.daily_go_live_audit import run_daily_go_live_audit
from app.tasks.zoom_call_sync import sync_zoom_calls

logger = logging.getLogger(__name__)

# Railway containers run in UTC. Without an explicit timezone, "0 7 * * 1-5"
# fires at 7:01 AM UTC (~midnight Pacific) instead of the intended 7:01 AM
# Pacific — same class of naive-timezone bug as the heartbeat staleness check.
_SCHEDULE_TIMEZONE = ZoneInfo("America/Los_Angeles")

_scheduler = BackgroundScheduler()


def _run_daily_go_live_audit_job() -> None:
    db = get_session_factory()()
    try:
        run_daily_go_live_audit(db)
    finally:
        db.close()


def _run_zoom_call_sync_job() -> None:
    """No target_date override -- see zoom_call_sync_cron's docstring in
    config.py for why letting sync_zoom_calls default to its own
    UTC-"yesterday" is exactly what makes this land on the SAME Pacific
    calendar day this job fires on, not a day behind. Logged explicitly
    (2026-09-29) since this was previously manual-only and had no scheduler
    coverage at all to mirror the go-live audit job's pattern against."""
    db = get_session_factory()()
    try:
        result = sync_zoom_calls(db)
        logger.info("zoom-call-sync: %s", result)
    except Exception:
        logger.exception("zoom-call-sync: scheduled run failed")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    settings = get_settings()
    audit_trigger = CronTrigger.from_crontab(settings.daily_go_live_audit_cron, timezone=_SCHEDULE_TIMEZONE)
    _scheduler.add_job(_run_daily_go_live_audit_job, audit_trigger, id="daily-go-live-audit", replace_existing=True)

    zoom_trigger = CronTrigger.from_crontab(settings.zoom_call_sync_cron, timezone=_SCHEDULE_TIMEZONE)
    _scheduler.add_job(_run_zoom_call_sync_job, zoom_trigger, id="zoom-call-sync", replace_existing=True)

    _scheduler.start()
    return _scheduler
