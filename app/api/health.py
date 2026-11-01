"""Liveness/readiness endpoints for observability and platform health checks."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import ensure_database_reached

router = APIRouter(tags=["health"])
log = get_logger("health")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": settings.APP_NAME, "env": settings.ENV}


@router.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness: this process has reached the database at least once.

    Queries only until the first success, then answers from memory -- see
    `ensure_database_reached` for why it must not ping on every probe.
    """
    try:
        await ensure_database_reached()
    except Exception as exc:
        # The type only: a driver's message can carry the host, and this route is public.
        log.warning("ready_database_unreachable", error=type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unreachable") from None
    return {"status": "ready"}
