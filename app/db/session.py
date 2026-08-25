"""Async SQLAlchemy/SQLModel engine and session factory."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings

engine = create_async_engine(
    settings.database_url_str,
    echo=settings.DB_ECHO,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
)

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
