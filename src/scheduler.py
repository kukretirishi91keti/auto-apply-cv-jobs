"""Daily job scheduling using APScheduler."""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import AppConfig

logger = logging.getLogger(__name__)


def create_scheduler(config: AppConfig, job_func: callable) -> BlockingScheduler:  # type: ignore[valid-type]
    """Create a blocking scheduler that runs the job function daily."""
    scheduler = BlockingScheduler()

    trigger = CronTrigger(
        hour=config.schedule.cron_hour,
        minute=config.schedule.cron_minute,
        timezone=config.schedule.timezone,
    )

    scheduler.add_job(
        job_func,
        trigger=trigger,
        id="daily_auto_apply",
        name="Daily Auto-Apply Job Run",
        replace_existing=True,
    )

    logger.info(
        "Scheduler configured: daily at %02d:%02d %s",
        config.schedule.cron_hour,
        config.schedule.cron_minute,
        config.schedule.timezone,
    )

    return scheduler
