"""Producer side of the queue: enqueue jobs from request context.

Celery's `.delay()` is synchronous Redis I/O. It is a millisecond, but this runs inside
FastAPI's event loop, so it is pushed to a thread rather than blocking every other
request served by the same worker.
"""
from __future__ import annotations

import asyncio
import uuid


async def enqueue_process_item(item_id: uuid.UUID) -> None:
    from app.queue.tasks import process_item

    await asyncio.to_thread(process_item.delay, str(item_id))


async def enqueue_finalize_run(provider_run_id: str) -> None:
    from app.queue.tasks import finalize_run

    await asyncio.to_thread(finalize_run.delay, provider_run_id)


async def close_pool() -> None:
    """Kept for the app lifespan hook. Celery holds no connection to close here."""
    return None
