"""Cross-cutting HTTP middleware: request IDs, security headers, basic rate limit."""
from __future__ import annotations

import time
import uuid

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core import rate_limit
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
    """Per-minute fixed-window request cap, keyed by client IP, counted in Redis.

    It used to keep its counts in a process dict, which was wrong in two ways and only
    one of them was written down. The known one: a count per process is a count per
    *replica* and per uvicorn worker, so the real limit was the configured one multiplied
    by the concurrency. The unwritten one was worse -- the dict was never pruned, so every
    distinct client address became a permanent entry and the limiter was a slow memory
    leak reachable from the internet by changing IP.

    Redis fixes both, and the counting itself is `core/rate_limit.consume`, which the
    per-user caps already use: one implementation of "count things in a window", one set
    of fail-open semantics. **Fails open on purpose** -- Redis being unreachable is
    already an outage of the queue, and turning it into "nobody may make a request"
    doubles the blast radius of a broker problem to protect against a cost the outage
    already bounds.

    Provider callbacks are exempt. Telegram and Apify deliver from a small pool of their
    own addresses, so every user's traffic collapses onto one key: a busy bot would trip
    the limit, and both providers treat a 429 as a delivery failure and retry it, which
    turns the limit into a self-amplifying backlog. Those endpoints are gated by a shared
    secret instead, and the real per-user limit lives in the worker where the sender is
    actually known.
    """

    #: Seconds. The window this cap has always used; named rather than inlined because it
    #: is also the `Retry-After` the client is told to wait.
    WINDOW = 60

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path.startswith(_WEBHOOK_PREFIX):
            return await call_next(request)
        client = request.client.host if request.client else "anon"
        if not await rate_limit.consume(
            "ip", client, settings.RATE_LIMIT_PER_MINUTE, window=self.WINDOW
        ):
            return JSONResponse(
                {"detail": "Rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(self.WINDOW)},
            )
        return await call_next(request)
