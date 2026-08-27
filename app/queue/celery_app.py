"""Celery application.

Prefork workers, Redis broker and result backend. Every task body is async — the bridge
is `asyncio.run` inside a sync task (see `tasks.py`), which is why database work goes
through `task_session()` and its NullPool rather than the shared engine.
"""
from __future__ import annotations

from typing import Any

from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, worker_process_init, worker_process_shutdown

from app.core.config import settings
from app.core.logging import configure_logging, start_log_sink, stop_log_sink

celery_app = Celery(
    "recall",
    broker=settings.redis_url_str,
    backend=settings.redis_url_str,
    include=["app.queue.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # A task is acknowledged only after it finishes, so a worker killed mid-job returns
    # the message to the queue instead of dropping the user's capture.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    task_soft_time_limit=settings.CELERY_TASK_TIME_LIMIT - 30,
    result_expires=3600,
    beat_schedule={
        # Catches runs whose webhook never arrived. Without it a lost callback leaves an
        # item stuck in `processing` forever, visible only in a database query.
        "sweep-stale-extraction-runs": {
            "task": "app.queue.tasks.sweep_stale_runs",
            "schedule": crontab(minute="*/5"),
        },
        # Expired sessions are dead weight, not evidence: once a row is past its
        # expiry it can neither be redeemed nor prove a replay. Daily is plenty --
        # nothing depends on the cleanup being timely.
        "purge-expired-sessions": {
            "task": "app.queue.tasks.purge_expired_sessions",
            "schedule": crontab(hour="4", minute="15"),
        },
        # Same reasoning for Telegram link tokens: an expired one cannot be redeemed,
        # so it is only table growth.
        "purge-expired-telegram-tokens": {
            "task": "app.queue.tasks.purge_expired_telegram_tokens",
            "schedule": crontab(hour="4", minute="30"),
        },
    },
)


# Per child process, not at import: prefork forks *after* this module loads, and an fd
# inherited across fork would have every child appending through one shared file offset.
# In dev this is what puts the worker's events in logs/worker-<date>.jsonl.
@worker_process_init.connect
def _start_worker_log_sink(**_kwargs: Any) -> None:
    configure_logging(source="worker")
    start_log_sink()


@worker_process_shutdown.connect
def _stop_worker_log_sink(**_kwargs: Any) -> None:
    stop_log_sink()


@beat_init.connect
def _start_beat_log_sink(**_kwargs: Any) -> None:
    configure_logging(source="beat")
    start_log_sink()
