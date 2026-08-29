"""FastAPI application factory and ASGI entrypoint.

Run: uvicorn app.main:app --reload
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import (
    configure_logging,
    get_logger,
    start_log_sink,
    stop_log_sink,
)
from app.core.middleware import (
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.queue.client import close_pool
from app.services.telegram.webhook import can_register, ensure_registered_quietly

log = get_logger("app")

#: A control-plane ping is a broadcast with a timeout, not a lookup: it costs exactly
#: this long when nothing answers, and boot must not wait longer than that on telemetry.
_WORKER_PING_TIMEOUT = 1.0


async def _warn_if_no_worker() -> None:
    """Say out loud, once, whether anything is consuming the queue."""
    import asyncio

    from app.queue.celery_app import celery_app

    try:
        replies = await asyncio.to_thread(
            celery_app.control.ping, timeout=_WORKER_PING_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 - a broker that is down is its own warning
        log.warning("celery_ping_failed", error=type(exc).__name__)
        return

    if replies:
        log.info("celery_workers_online", count=len(replies))
    else:
        log.warning(
            "celery_no_workers",
            hint="queued work will not run until `make worker` is started",
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging(source="api")
    # Opens logs/api-<date>.jsonl and prunes expired files. No-op outside ENV=dev.
    start_log_sink()
    startup: dict[str, str] = {"env": settings.ENV}
    if app.docs_url and app.openapi_url:
        base = settings.BACKEND_URL.rstrip("/")
        startup["docs"] = f"{base}{app.docs_url}"
        startup["openapi"] = f"{base}{app.openapi_url}"
    else:
        startup["docs"] = f"disabled (ENV={settings.ENV})"
    log.info("startup", **startup)

    # Telegram remembers one delivery URL per bot and nothing re-checks it, so a changed
    # public URL makes the bot silently unreachable -- it keeps taking messages and
    # answering none. Reconciled here so a restart is the fix, not a remembered command.
    if settings.STARTUP_SELF_CHECK and can_register():
        # `result`, not `status`: `status` is a promoted record key in `log_sink`, so a
        # value logged under it is swallowed and the line arrives with an empty context.
        log.info("telegram_webhook_startup", result=await ensure_registered_quietly())

    # The webhook only *queues*; a reply needs a worker. With none running the API still
    # answers Telegram 202, Telegram is satisfied, and the user waits forever for a reply
    # that no process is going to write. That failure is invisible without this line.
    if settings.STARTUP_SELF_CHECK:
        await _warn_if_no_worker()

    yield
    await close_pool()
    log.info("shutdown")
    # Last thing: the line above is written before the file is closed.
    stop_log_sink()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        # Interactive docs and the raw schema map the entire API surface, so they are
        # withheld in production. `app.openapi()` still works for offline export.
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )

    # Order matters: outermost first.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)
    return app


app = create_app()
