"""APScheduler wiring: sender tick, reply polling, daily report, nightly backup.

Any scheduled job that raises an unhandled exception is caught by a job-error
listener that logs an event and alerts on Telegram, so a silent background
failure never goes unnoticed.
"""
import asyncio
import logging
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from engine.backups import nightly_backup
from engine.config import get_settings
from engine.replies import process_inbox
from engine.reporter import send_daily_report
from engine.sender import sender_tick
from engine.util import parse_hhmm

log = logging.getLogger("scheduler")


def _on_job_error(event) -> None:
    """APScheduler job-error listener: log + Telegram alert, never re-raise."""
    exc = event.exception
    log.error("Scheduled job %s raised: %s", event.job_id, exc, exc_info=exc)
    try:
        from db.session import new_session
        from engine.events import log_event

        session = new_session()
        try:
            log_event(session, "scheduler",
                      f"Job '{event.job_id}' failed: {exc}", level="ERROR")
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — logging must never crash the listener
        pass
    try:
        from bot.notify import notify

        asyncio.get_event_loop().create_task(
            notify(f"Scheduled job '{event.job_id}' failed: {str(exc)[:300]}"))
    except Exception:  # noqa: BLE001
        pass


def build_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(sender_tick, "interval", seconds=60, id="sender_tick",
                      max_instances=1, coalesce=True)
    scheduler.add_job(process_inbox, "interval",
                      minutes=max(1, settings.reply_poll_minutes),
                      id="reply_watcher", max_instances=1, coalesce=True)
    report_at = parse_hhmm(settings.report_time)
    scheduler.add_job(
        send_daily_report,
        CronTrigger(hour=report_at.hour, minute=report_at.minute,
                    timezone=ZoneInfo(settings.report_timezone)),
        id="daily_report",
    )
    scheduler.add_job(nightly_backup,
                      CronTrigger(hour=3, minute=30, timezone="UTC"),
                      id="nightly_backup", max_instances=1, coalesce=True)
    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    log.info("Scheduler ready: sender 60s, replies %sm, report %s %s, backup 03:30 UTC",
             settings.reply_poll_minutes, settings.report_time, settings.report_timezone)
    return scheduler
