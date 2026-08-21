"""Producer side of the queue: enqueue jobs from API/request context.

A single lazily-created ARQ Redis pool is reused across requests.
"""
from __future__ import annotations

import uuid

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import settings

_pool: ArqRedis | None = None


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url_str)


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(_redis_settings())
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def enqueue_process_item(item_id: uuid.UUID) -> None:
    pool = await get_pool()
    await pool.enqueue_job("process_item", str(item_id))
