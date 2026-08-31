"""Async SQLAlchemy/SQLModel engine and session factory."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("db")

engine = create_async_engine(
    settings.database_url_str,
    echo=settings.DB_ECHO,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    # `pool_pre_ping` costs one full round trip per checkout. On a managed database in
    # another region that is the same price as the query the request came to run --
    # measured at ~290ms against Neon ap-southeast-1, which is why endpoints doing a
    # single lookup were answering in ~600ms. `pool_recycle` covers the same failure by
    # discarding a connection that has been idle longer than the shortest timeout in
    # front of Postgres, rather than testing every connection to catch the rare stale one.
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    pool_recycle=settings.DB_POOL_RECYCLE,
)


async def warm_pool(connections: int | None = None) -> int:
    """Open (and return to the pool) a few connections before the first request.

    A cold connection to a managed Postgres is a TLS handshake plus authentication --
    measured at ~1.85s against Neon, versus ~0.1ms to check one out of a warm pool. That
    cost lands on whichever user arrives first after a deploy or an idle period, which is
    exactly the page load that gets read as "the app is broken".

    Failures are swallowed and logged: the database being unreachable at boot is a real
    situation (it is a separate service, and Neon suspends an idle compute), and it must
    degrade to a slow first request rather than an API that refuses to start.
    """
    count = settings.DB_POOL_WARMUP if connections is None else connections
    if count <= 0:
        return 0
    import asyncio

    async def _open() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    results = await asyncio.gather(
        *(_open() for _ in range(count)), return_exceptions=True
    )
    opened = sum(1 for r in results if not isinstance(r, BaseException))
    if opened < count:
        first = next((r for r in results if isinstance(r, BaseException)), None)
        log.warning(
            "db_pool_warmup_partial",
            opened=opened,
            requested=count,
            error=type(first).__name__ if first else None,
        )
    return opened

SessionFactory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a transactional session."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def task_session() -> AsyncGenerator[AsyncSession, None]:
    """Session for a Celery task, with an engine scoped to this call.

    The module-level `engine` above keeps a connection pool. Celery prefork workers run
    each task through `asyncio.run`, which creates and then *closes* a fresh event loop —
    and asyncpg connections are bound to the loop that opened them. Reusing the shared
    pool across tasks therefore hands the second task a connection whose loop is dead,
    which surfaces as "attached to a different loop" or a silent hang.

    NullPool sidesteps it entirely: connect on entry, disconnect on exit, nothing cached
    across loops. The cost is one connect per task, which is noise next to a 30s scrape.
    """
    task_engine = create_async_engine(
        settings.database_url_str,
        echo=settings.DB_ECHO,
        poolclass=NullPool,
    )
    factory = async_sessionmaker(
        task_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    try:
        async with factory() as session:
            yield session
    finally:
        await task_engine.dispose()
