"""Producer side of the queue: enqueue jobs from request context.

Celery's `.delay()` is synchronous Redis I/O. It is a millisecond, but this runs inside
FastAPI's event loop, so it is pushed to a thread rather than blocking every other
request served by the same worker.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any


async def enqueue_process_item(item_id: uuid.UUID) -> None:
    from app.queue.tasks import process_item

    await asyncio.to_thread(process_item.delay, str(item_id))


async def enqueue_finalize_run(provider_run_id: str) -> None:
    from app.queue.tasks import finalize_run

    await asyncio.to_thread(finalize_run.delay, provider_run_id)


async def enqueue_derive_connections(item_id: uuid.UUID) -> None:
    from app.queue.tasks import derive_connections

    await asyncio.to_thread(derive_connections.delay, str(item_id))


async def enqueue_telegram_update(update: dict[str, Any]) -> None:
    """Hand a raw Telegram update to the worker.

    The whole update travels through Redis rather than an id, because Telegram keeps no
    fetchable copy -- unlike an Apify run, there is nothing to re-read later. It is
    treated as untrusted input at every point it is read.
    """
    from app.queue.tasks import handle_telegram_update

    await asyncio.to_thread(handle_telegram_update.delay, update)


async def enqueue_telegram_delivery(item_id: uuid.UUID) -> None:
    from app.queue.tasks import deliver_telegram_result

    await asyncio.to_thread(deliver_telegram_result.delay, str(item_id))


async def enqueue_shadow_agent_turn(
    user_id: str, question: str, session_id: str, surface: str, router_lane: str
) -> None:
    """Queue a shadow agent run for a turn that has already been answered."""
    from app.queue.tasks import shadow_agent_turn

    await asyncio.to_thread(
        shadow_agent_turn.delay, user_id, question, session_id, surface, router_lane
    )


async def close_pool() -> None:
    """Kept for the app lifespan hook. Celery holds no connection to close here."""
    return None
