"""Twice-daily scan scheduling (APScheduler, in-process).

Two scan jobs run each day at the scan_time_1 / scan_time_2 settings (HH:MM,
container-local time — set TZ in the compose file). Settings changes re-arm
the schedule. This replaces the external scheduler/cron the standalone
script used to rely on.
"""

from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import scanner

logger = logging.getLogger(__name__)

JOB_IDS = ("redsave-scan-1", "redsave-scan-2")
K_TIME_1 = "scan_time_1"
K_TIME_2 = "scan_time_2"
DEFAULT_TIME_1 = "08:00"
DEFAULT_TIME_2 = "20:00"

scheduler = BackgroundScheduler()


def _job() -> None:
    """The scheduled scan — run_scan returns None while another scan runs."""
    thread = threading.Thread(target=scanner.run_scan, daemon=True)
    thread.start()


def _parse_time(raw: str | None, default: str) -> tuple[int, int]:
    try:
        hour_s, minute_s = (raw or default).strip().split(":")
        hour, minute = int(hour_s), int(minute_s)
        assert 0 <= hour <= 23 and 0 <= minute <= 59
        return hour, minute
    except (ValueError, AssertionError, AttributeError):
        logger.warning("invalid scan time %r — falling back to %s", raw, default)
        hour_s, minute_s = default.split(":")
        return int(hour_s), int(minute_s)


def schedule_from_settings() -> None:
    """(Re)arm both daily scans from the current settings. Call on startup and
    whenever the schedule settings change."""
    for job_id in JOB_IDS:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
    times = [
        _parse_time(scanner.get_setting_value(K_TIME_1, DEFAULT_TIME_1), DEFAULT_TIME_1),
        _parse_time(scanner.get_setting_value(K_TIME_2, DEFAULT_TIME_2), DEFAULT_TIME_2),
    ]
    for job_id, (hour, minute) in zip(JOB_IDS, times):
        scheduler.add_job(
            _job, CronTrigger(hour=hour, minute=minute), id=job_id,
            misfire_grace_time=3600, coalesce=True,
        )
        logger.info("RedSaves scan scheduled daily at %02d:%02d", hour, minute)


def next_run_times() -> list[str]:
    out = []
    for job_id in JOB_IDS:
        job = scheduler.get_job(job_id)
        if job and job.next_run_time:
            out.append(job.next_run_time.strftime("%Y-%m-%d %H:%M"))
    return sorted(out)


def start() -> None:
    if not scheduler.running:
        scheduler.start()
    schedule_from_settings()


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
