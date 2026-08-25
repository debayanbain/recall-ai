"""Celery application.

Prefork workers, Redis broker and result backend. Every task body is async — the bridge
is `asyncio.run` inside a sync task (see `tasks.py`), which is why database work goes
through `task_session()` and its NullPool rather than the shared engine.
"""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

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
    },
)
