from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.db import get_session_factory
from app.tasks.daily_go_live_audit import run_daily_go_live_audit

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


def start_scheduler() -> BackgroundScheduler:
    settings = get_settings()
    trigger = CronTrigger.from_crontab(settings.daily_go_live_audit_cron, timezone=_SCHEDULE_TIMEZONE)
    _scheduler.add_job(_run_daily_go_live_audit_job, trigger, id="daily-go-live-audit", replace_existing=True)
    _scheduler.start()
    return _scheduler
