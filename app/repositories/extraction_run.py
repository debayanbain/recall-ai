"""Deferred-extraction run tracking."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.extraction_run import ExtractionRun, RunStatus


class ExtractionRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, run: ExtractionRun) -> ExtractionRun:
        self.session.add(run)
        await self.session.flush()
        await self.session.refresh(run)
        return run

    async def get_by_provider_run(
        self, provider: str, provider_run_id: str
    ) -> ExtractionRun | None:
        result = await self.session.exec(
            select(ExtractionRun).where(
                ExtractionRun.provider == provider,
                ExtractionRun.provider_run_id == provider_run_id,
            )
        )
        return result.first()

    async def list_stale(self, older_than_minutes: int, limit: int = 50) -> list[ExtractionRun]:
        """Runs still `running` past the deadline — a webhook that never arrived.

        The sweeper's whole purpose: a lost callback would otherwise leave an item
        `processing` forever, invisible to everyone except a database query.
        """
        cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
        result = await self.session.exec(
            select(ExtractionRun)
            .where(ExtractionRun.status == RunStatus.running)
            .where(ExtractionRun.created_at < cutoff)
            .limit(limit)
        )
        return list(result.all())

    async def mark(
        self, run: ExtractionRun, status: RunStatus, error: str | None = None
    ) -> ExtractionRun:
        run.status = status
        run.error = error[:500] if error else None
        run.finished_at = datetime.now(UTC)
        self.session.add(run)
        await self.session.flush()
        return run

    async def get_item_id(self, run_id: uuid.UUID) -> uuid.UUID | None:
        run = await self.session.get(ExtractionRun, run_id)
        return run.vault_item_id if run else None
