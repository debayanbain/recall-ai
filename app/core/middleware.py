"""Cross-cutting HTTP middleware: request IDs, security headers, basic rate limit."""
from __future__ import annotations

import time
import uuid

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("http")

# Secret-gated provider callbacks, exempt from the IP rate limiter. See
# RateLimitMiddleware.
_WEBHOOK_PREFIX = f"{settings.API_V1_PREFIX}/webhooks/"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a correlation/request id, bind logging context, log timing."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            path=request.url.path,
            method=request.method,
        )
        request.state.request_id = request_id
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled_error")
            raise
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["x-request-id"] = request_id
        log.info("request", status=response.status_code, duration_ms=elapsed_ms)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Helmet-equivalent security headers."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-memory fixed-window rate limiter keyed by client IP.

    Good enough for 10 users. Swap to Redis token-bucket before scaling out.

    Provider callbacks are exempt. Telegram and Apify deliver from a small pool of their
    own addresses, so every user's traffic collapses onto one key: a busy bot would trip
    the limit, and both providers treat a 429 as a delivery failure and retry it, which
    turns the limit into a self-amplifying backlog. Those endpoints are gated by a shared
    secret instead, and the real per-user limit lives in the worker where the sender is
    actually known.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._hits: dict[str, tuple[int, float]] = {}

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path.startswith(_WEBHOOK_PREFIX):
            return await call_next(request)
        client = request.client.host if request.client else "anon"
        now = time.time()
        window = 60.0
        count, started = self._hits.get(client, (0, now))
        if now - started > window:
            count, started = 0, now
        count += 1
        self._hits[client] = (count, started)
        if count > settings.RATE_LIMIT_PER_MINUTE:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                {"detail": "Rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        return await call_next(request)
