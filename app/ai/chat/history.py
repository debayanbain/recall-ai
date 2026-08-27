"""Short-term conversation memory, in Redis.

Redis rather than Postgres because this is disposable: a chat that has gone quiet for an
hour should start fresh rather than resume mid-thought, and a TTL expresses that in one
line where a table would need a purge task, a migration and a retention decision.

History is loaded and appended explicitly rather than through
`RunnableWithMessageHistory`. That wrapper exists to hide exactly this bookkeeping, but
it hides it behind a sync/async adapter that runs the sync path in a thread pool -- and
these calls happen inside `asyncio.run` in a prefork Celery child, which is the one place
a hidden thread hand-off is expensive to debug. Forty lines of explicit code is the
cheaper trade.

Turns are trimmed on write, so a long-running chat cannot grow the prompt without bound.
"""
from __future__ import annotations

import json

import redis.asyncio as redis
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("telegram")

#: Kept small on purpose. The answer is grounded in retrieved memories, so history only
#: has to carry pronouns and follow-ups ("what about last month?"), not the subject matter.
_MAX_TURNS = 6


def _key(session_id: str) -> str:
    return f"tg:chat:{session_id}"


async def load(session_id: str) -> list[BaseMessage]:
    """Recent turns, oldest first. Returns nothing at all if Redis is unreachable."""
    client = redis.from_url(settings.redis_url_str)  # type: ignore[no-untyped-call]
    try:
        raw = await client.lrange(_key(session_id), -_MAX_TURNS * 2, -1)
    except Exception as exc:  # noqa: BLE001 - history is an enhancement, not a dependency
        log.warning("telegram_history_load_failed", error=type(exc).__name__)
        return []
    finally:
        await client.aclose()

    messages: list[BaseMessage] = []
    for entry in raw:
        try:
            payload = json.loads(entry)
            role, content = payload["role"], payload["content"]
        except (ValueError, KeyError, TypeError):
            continue
        if role == "human":
            messages.append(HumanMessage(content=content))
        elif role == "ai":
            messages.append(AIMessage(content=content))
    return messages


async def append(session_id: str, question: str, answer: str) -> None:
    """Record one exchange and re-arm the TTL."""
    client = redis.from_url(settings.redis_url_str)  # type: ignore[no-untyped-call]
    key = _key(session_id)
    try:
        pipe = client.pipeline()
        pipe.rpush(
            key,
            json.dumps({"role": "human", "content": question}),
            json.dumps({"role": "ai", "content": answer}),
        )
        # Trim on write so the list is bounded even if the TTL is never reached.
        pipe.ltrim(key, -_MAX_TURNS * 2, -1)
        pipe.expire(key, settings.TELEGRAM_CHAT_HISTORY_TTL_SECONDS)
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001 - losing history must not lose the answer
        log.warning("telegram_history_append_failed", error=type(exc).__name__)
    finally:
        await client.aclose()


async def clear(session_id: str) -> None:
    client = redis.from_url(settings.redis_url_str)  # type: ignore[no-untyped-call]
    try:
        await client.delete(_key(session_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram_history_clear_failed", error=type(exc).__name__)
    finally:
        await client.aclose()
