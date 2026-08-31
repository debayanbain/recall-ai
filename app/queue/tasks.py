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
from app.models.base import ProcessingStatus
from app.models.extraction_run import RunStatus
from app.queue.celery_app import celery_app
from app.queue.client import enqueue_process_item, enqueue_telegram_delivery
from app.repositories.extraction_run import ExtractionRunRepository
from app.repositories.vault import VaultRepository
from app.services.apify import get_run
from app.services.processing_service import ProcessingService
from app.storage import get_storage

# The worker overrides this from `worker_process_init` (see celery_app.py); this call
# covers module import and any process that imports tasks without those signals.
configure_logging(source="worker")
log = get_logger("tasks")

#: Apify run statuses that mean "there is a dataset to read".
_SUCCESS = "SUCCEEDED"


async def _notify_surface(item_id: uuid.UUID, session: Any) -> None:
    """Tell the capture surface an item finished, if it came from one.

    Called after the commit, so the delivery task always finds a visible row. Kept
    here rather than in `ProcessingService` because the pipeline has no business
    knowing which chat surface a save arrived from.
    """
    item = await VaultRepository(session).get_unscoped(item_id)
    if item is None:
        return
    if (item.item_metadata or {}).get("source") != "telegram":
        return
    try:
        await enqueue_telegram_delivery(item.id)
    except Exception as exc:  # noqa: BLE001 - the item is saved either way
        log.warning("telegram_delivery_enqueue_failed", error=type(exc).__name__)


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
        if self.request.retries >= self.max_retries:
            # Last attempt. Without this the capture surface never hears back and the
            # user is left watching an acknowledgement that never resolves.
            try:
                asyncio.run(_notify_final_failure(item_id))
            except Exception:  # noqa: BLE001 - the retry result matters more
                log.warning("notify_final_failure_failed", item_id=item_id)
        raise self.retry(
            exc=RuntimeError(f"{type(exc).__name__}: {str(exc)[:300]}")
        ) from None


async def _notify_final_failure(item_id: str) -> None:
    async with task_session() as session:
        await _notify_surface(uuid.UUID(item_id), session)


async def _process_item(item_id: str) -> str | None:
    async with task_session() as session:
        service = ProcessingService(
            VaultRepository(session), ExtractionRunRepository(session), get_storage()
        )
        try:
            run_id = await service.process(uuid.UUID(item_id))
        except Exception:
            # The failure bookkeeping lives on the row; committing it is not optional or
            # the retry loses the reason and the item looks merely `pending`.
            await session.commit()
            raise
        await session.commit()
        if run_id is None:
            # A deferred extraction is not finished yet; its reply is sent by
            # `_finalize_run` instead.
            await _notify_surface(uuid.UUID(item_id), session)
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
            await _notify_surface(run.vault_item_id, session)
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
        await _notify_surface(run.vault_item_id, session)


@celery_app.task(name="app.queue.tasks.sweep_stranded_items")
def sweep_stranded_items() -> dict[str, int]:
    """Beat task: nothing captured is allowed to sit in limbo unnoticed.

    Celery's own retries only cover a task that *ran and raised*. Two failure shapes slip
    past them entirely, and both end as a card the user watches forever:

    * **Stuck in `processing`** — the worker was SIGKILLed, OOM-killed or lost its host
      mid-task. `acks_late` returns the message to the broker, but nothing resets the row,
      so the item claims to be working while no one is working on it. There is no safe way
      to know whether the AI calls half-happened, so it is marked `failed` with a sentence
      the owner can act on, which is what puts the Retry button on the page.
    * **Stuck in `pending`** — it was never queued at all. The usual cause is
      `vault_enqueue_failed`: the row committed while Redis was unreachable. That is
      re-drivable, so it is re-queued rather than failed, up to `MAX_SWEEP_REQUEUES` —
      without a ceiling, an item that kills the worker on load is re-driven forever.

    Returns counts rather than raising: one bad row must never stop the sweep.
    """
    return asyncio.run(_sweep_stranded_items())


async def _sweep_stranded_items() -> dict[str, int]:
    from app.core.config import settings

    requeued = 0
    failed = 0

    async with task_session() as session:
        repo = VaultRepository(session)

        # --- never picked up -------------------------------------------------------
        for item in await repo.list_stranded(
            ProcessingStatus.pending, settings.STUCK_PENDING_MINUTES
        ):
            if item.retry_count >= settings.MAX_SWEEP_REQUEUES:
                item.processing_status = ProcessingStatus.failed
                item.processing_error = (
                    "This never reached the processing queue. Try again — if it keeps "
                    "happening, the background service needs attention."
                )
                failed += 1
                log.warning("sweep_pending_exhausted", item_id=str(item.id))
                continue
            item.retry_count += 1
            try:
                await enqueue_process_item(item.id)
            except Exception as exc:  # noqa: BLE001 - the queue is the thing that is down
                log.warning(
                    "sweep_requeue_failed", item_id=str(item.id), error=type(exc).__name__
                )
                continue
            requeued += 1
            log.info("sweep_requeued", item_id=str(item.id), attempt=item.retry_count)

        # --- claimed, then abandoned -----------------------------------------------
        for item in await repo.list_stranded(
            ProcessingStatus.processing, settings.STUCK_PROCESSING_MINUTES
        ):
            item.processing_status = ProcessingStatus.failed
            item.processing_error = (
                "Processing stopped partway through — the background worker went away "
                "before it finished. Nothing was lost; try again."
            )
            item.retry_count += 1
            failed += 1
            log.warning(
                "sweep_marked_stuck_failed",
                item_id=str(item.id),
                minutes=settings.STUCK_PROCESSING_MINUTES,
            )

        await session.commit()

    if requeued or failed:
        log.info("sweep_stranded_items", requeued=requeued, failed=failed)
    return {"requeued": requeued, "failed": failed}


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


@celery_app.task(name="app.queue.tasks.purge_expired_sessions")
def purge_expired_sessions() -> int:
    """Beat task: delete refresh-session rows that can no longer prove anything.

    Rotation writes a row per refresh, so an active user accumulates one every
    ACCESS_TOKEN_EXPIRE_MINUTES. Rows are kept past revocation on purpose -- that is what
    makes a replayed token detectable -- but once a row is expired it can neither be
    redeemed nor tell us anything, so it is only table growth.

    Deletes strictly by `expires_at`, never by `revoked_at`: a revoked-but-unexpired row
    is exactly the evidence the reuse check needs.
    """
    return asyncio.run(_purge_expired_sessions())


async def _purge_expired_sessions() -> int:
    from datetime import UTC, datetime

    from app.repositories.user_session import UserSessionRepository

    async with task_session() as session:
        deleted = await UserSessionRepository(session).delete_expired(datetime.now(UTC))
        await session.commit()
        if deleted:
            log.info("sessions_purged", deleted=deleted)
        return deleted


@celery_app.task(
    name="app.queue.tasks.handle_telegram_update",
    bind=True,
    max_retries=2,
    default_retry_delay=15,
    acks_late=True,
)
def handle_telegram_update(self: Any, update: dict[str, Any]) -> None:
    """Process one Telegram update: authorise the sender, act, reply.

    Retried at most twice, and only for infrastructure failures. A malformed update is
    dropped inside the body rather than raised, because Telegram redelivers the same
    bytes and a message we cannot parse will never parse.
    """
    try:
        asyncio.run(_handle_telegram_update(update))
    except Exception as exc:
        log.warning("telegram_update_failed", error=type(exc).__name__)
        raise self.retry(
            exc=RuntimeError(f"{type(exc).__name__}: {str(exc)[:300]}")
        ) from None


async def _handle_telegram_update(update: dict[str, Any]) -> None:
    import structlog

    # The HTTP middleware stamps a request_id on API traffic; nothing does for an update
    # that arrives here through the queue, so one line of a bot conversation could not be
    # correlated with the model calls it caused. Telegram's own update_id is the natural
    # key -- it is unique per bot and already identifies this delivery.
    structlog.contextvars.bind_contextvars(
        request_id=f"tg-{update.get('update_id', 'unknown')}"
    )

    from app.repositories.telegram import (
        TelegramAccountRepository,
        TelegramLinkTokenRepository,
    )
    from app.services.telegram.client import TelegramClient
    from app.services.telegram.dispatch import TelegramDispatcher
    from app.services.telegram.linking import TelegramLinkService
    from app.services.telegram.typing import chat_id_of, typing_action
    from app.services.vault_service import VaultService
    from app.storage import get_storage

    # Read before any work starts, and from the raw payload: the indicator is the only
    # sign the message arrived, and a reply can be several seconds away -- an embedding,
    # a planner call and an answer call for a question, a download and an upload for a
    # file. Silence for that long reads as a bot that never received the message, and
    # the user sends it again.
    typing_chat_id = chat_id_of(update)

    async with TelegramClient() as client, task_session() as session:
        links = TelegramLinkService(
            TelegramAccountRepository(session), TelegramLinkTokenRepository(session)
        )
        vault = VaultService(VaultRepository(session), get_storage())
        dispatcher = TelegramDispatcher(
            links, vault, client, recall=_recall_responder(vault.repo)
        )

        async with typing_action(client, typing_chat_id):
            result = await dispatcher.handle(update)
            # Commit before enqueuing anything: the worker that picks the job up runs in
            # a different transaction and must be able to see the row.
            await session.commit()

        for item_id in result.enqueue_item_ids:
            try:
                await enqueue_process_item(item_id)
            except Exception as exc:  # noqa: BLE001 - the item is saved either way
                log.warning("telegram_enqueue_failed", error=type(exc).__name__)

        if result.reply and result.chat_id:
            await client.send_message(
                result.chat_id, result.reply, reply_markup=result.reply_markup
            )


def _recall_responder(repo: VaultRepository) -> Any:
    """The retrieval half, if this deployment has a chat model configured.

    Imported lazily so the worker still starts -- and still captures -- when the
    LangChain extras are missing from the environment.
    """
    try:
        from app.services.recall_chat import build_recall_responder
    except ImportError:  # pragma: no cover - only when the extras are absent
        log.warning("recall_chat_unavailable")
        return None
    return build_recall_responder(repo)


@celery_app.task(
    name="app.queue.tasks.deliver_telegram_result",
    bind=True,
    max_retries=3,
    default_retry_delay=20,
    acks_late=True,
)
def deliver_telegram_result(self: Any, item_id: str) -> None:
    """Send the summary/category/tags reply once an item finishes processing."""
    try:
        asyncio.run(_deliver_telegram_result(item_id))
    except Exception as exc:
        log.warning(
            "telegram_delivery_failed", item_id=item_id, error=type(exc).__name__
        )
        raise self.retry(
            exc=RuntimeError(f"{type(exc).__name__}: {str(exc)[:300]}")
        ) from None


async def _deliver_telegram_result(item_id: str) -> None:
    from app.repositories.telegram import TelegramAccountRepository
    from app.services.telegram import formatting
    from app.services.telegram.client import TelegramClient

    async with task_session() as session:
        item = await VaultRepository(session).get_unscoped(uuid.UUID(item_id))
        if item is None:
            log.warning("telegram_delivery_missing_item", item_id=item_id)
            return

        # The chat address is re-derived from the owner's binding, never read from
        # `item_metadata`. A metadata value is writable by the pipeline and by any future
        # extractor; trusting it here would be a way to route one user's content into
        # another user's chat.
        account = await TelegramAccountRepository(session).get_for_user(item.user_id)
        if account is None:
            log.info("telegram_delivery_no_account", item_id=item_id)
            return

        async with TelegramClient() as client:
            await client.send_message(account.telegram_chat_id, formatting.result(item))


@celery_app.task(name="app.queue.tasks.purge_expired_telegram_tokens")
def purge_expired_telegram_tokens() -> int:
    """Beat task: drop link tokens that can no longer be redeemed.

    Deletes strictly by `expires_at`. A used-but-unexpired row is kept until it expires
    so a replayed link is still recognised as spent rather than as unknown.
    """
    return asyncio.run(_purge_expired_telegram_tokens())


async def _purge_expired_telegram_tokens() -> int:
    from datetime import UTC, datetime

    from app.repositories.telegram import TelegramLinkTokenRepository

    async with task_session() as session:
        deleted = await TelegramLinkTokenRepository(session).delete_expired(
            datetime.now(UTC)
        )
        await session.commit()
        if deleted:
            log.info("telegram_link_tokens_purged", deleted=deleted)
        return deleted
