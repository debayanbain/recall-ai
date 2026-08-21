"""ARQ worker definition. Run with: arq app.queue.worker.WorkerSettings"""
from __future__ import annotations

import uuid
from typing import Any

from arq.connections import RedisSettings

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.session import SessionFactory
from app.repositories.vault import VaultRepository
from app.services.processing_service import ProcessingService

log = get_logger("worker")


async def process_item(ctx: dict[str, Any], item_id: str) -> None:
    """Job: run the full enrichment pipeline for one vault item."""
    async with SessionFactory() as session:
        service = ProcessingService(VaultRepository(session))
        await service.process(uuid.UUID(item_id))
        await session.commit()


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    log.info("worker_startup")


async def shutdown(ctx: dict[str, Any]) -> None:
    log.info("worker_shutdown")


class WorkerSettings:
    functions = [process_item]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url_str)
    max_tries = 4
    job_timeout = 120
    keep_result = 3600
