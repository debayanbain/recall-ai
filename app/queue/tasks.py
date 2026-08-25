"""Celery tasks.

Each task is a thin sync shell around an async body. `asyncio.run` creates and closes a
fresh event loop per task, which is safe here only because `task_session()` builds a
NullPool engine inside that loop — the shared module-level pool would hand the next task
a connection belonging to a dead loop.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.core.logging import configure_logging, get_logger
from app.db.session import task_session
from app.models.extraction_run import RunStatus
from app.queue.celery_app import celery_app
from app.repositories.extraction_run import ExtractionRunRepository
from app.repositories.vault import VaultRepository
from app.services.apify import get_run
from app.services.processing_service import ProcessingService

configure_logging()
log = get_logger("tasks")

#: Apify run statuses that mean "there is a dataset to read".
_SUCCESS = "SUCCEEDED"


@celery_app.task(
    name="app.queue.tasks.process_item",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
)
def process_item(self: Any, item_id: str) -> str | None:
    """Entry point for a freshly captured item."""
    try:
        return asyncio.run(_process_item(item_id))
    except Exception as exc:
        # Provider SDK errors can hold unpicklable handles, and Celery serializes the
        # exception into the result backend. Re-raise a plain one.
        log.warning("process_item_failed", item_id=item_id, error=type(exc).__name__)
        raise self.retry(
            exc=RuntimeError(f"{type(exc).__name__}: {str(exc)[:300]}")
        ) from None


async def _process_item(item_id: str) -> str | None:
    async with task_session() as session:
        service = ProcessingService(
            VaultRepository(session), ExtractionRunRepository(session)
        )
        try:
            run_id = await service.process(uuid.UUID(item_id))
        except Exception:
            # The failure bookkeeping lives on the row; committing it is not optional or
            # the retry loses the reason and the item looks merely `pending`.
            await session.commit()
            raise
        await session.commit()
        return run_id


@celery_app.task(
    name="app.queue.tasks.finalize_run",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
)
def finalize_run(self: Any, provider_run_id: str) -> None:
    """Finish a deferred item once its provider run has ended."""
    try:
        asyncio.run(_finalize_run(provider_run_id))
    except Exception as exc:
        log.warning(
            "finalize_run_failed", run_id=provider_run_id, error=type(exc).__name__
        )
        raise self.retry(
            exc=RuntimeError(f"{type(exc).__name__}: {str(exc)[:300]}")
        ) from None


async def _finalize_run(provider_run_id: str) -> None:
    async with task_session() as session:
        runs = ExtractionRunRepository(session)
        run = await runs.get_by_provider_run("apify", provider_run_id)
        if run is None:
            # A callback for a run this deployment never started — ignore rather than
            # inventing work from an unknown id.
            log.warning("finalize_unknown_run", run_id=provider_run_id)
            return
        if run.status != RunStatus.running:
            log.info("finalize_run_already_terminal", run_id=provider_run_id)
            return

        service = ProcessingService(VaultRepository(session), runs)

        # Authoritative: never trust the webhook body for status or dataset id.
        detail = await get_run(provider_run_id)
        status = str(detail.get("status") or "")
        dataset_id = detail.get("defaultDatasetId")

        if status != _SUCCESS or not dataset_id:
            reason = f"Apify run ended as {status or 'UNKNOWN'}"
            await service.fail_item(run.vault_item_id, reason)
            await runs.mark(run, RunStatus.failed, reason)
            await session.commit()
            return

        items = await service.fetch_payload(run.vault_item_id, str(dataset_id))
        run.dataset_id = str(dataset_id)
        try:
            await service.finalize(run.vault_item_id, items)
        finally:
            # Mark terminal even on failure: a permanently broken payload must not be
            # re-driven by the sweeper every five minutes forever.
            await runs.mark(run, RunStatus.succeeded)
            await session.commit()


@celery_app.task(name="app.queue.tasks.sweep_stale_runs")
def sweep_stale_runs() -> int:
    """Beat task: rescue runs whose webhook never arrived."""
    return asyncio.run(_sweep_stale_runs())


async def _sweep_stale_runs() -> int:
    from app.core.config import settings

    async with task_session() as session:
        runs = ExtractionRunRepository(session)
        stale = await runs.list_stale(settings.EXTRACTION_RUN_TIMEOUT_MINUTES)
        if not stale:
            return 0

        service = ProcessingService(VaultRepository(session), runs)
        rescued = 0
        for run in stale:
            try:
                detail = await get_run(run.provider_run_id)
            except Exception as exc:  # noqa: BLE001 - one bad run must not stop the sweep
                log.warning(
                    "sweep_lookup_failed",
                    run_id=run.provider_run_id,
                    error=type(exc).__name__,
                )
                continue

            status = str(detail.get("status") or "")
            if status in ("READY", "RUNNING"):
                continue  # genuinely still working; leave it alone

            dataset_id = detail.get("defaultDatasetId")
            if status == _SUCCESS and dataset_id:
                # The webhook was lost but the data is there — finish the job.
                items = await service.fetch_payload(run.vault_item_id, str(dataset_id))
                run.dataset_id = str(dataset_id)
                await service.finalize(run.vault_item_id, items)
                await runs.mark(run, RunStatus.succeeded)
            else:
                reason = f"Apify run ended as {status or 'UNKNOWN'} (no callback received)"
                await service.fail_item(run.vault_item_id, reason)
                await runs.mark(run, RunStatus.timed_out, reason)
            rescued += 1

        await session.commit()
        log.info("sweep_completed", checked=len(stale), rescued=rescued)
        return rescued
