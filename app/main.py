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

log = get_logger("app")


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
