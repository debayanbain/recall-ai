"""Liveness/readiness endpoints for observability and platform health checks."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import SessionDep
from app.core.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": settings.APP_NAME, "env": settings.ENV}


@router.get("/ready")
async def ready(session: SessionDep) -> dict[str, str]:
    """Readiness probe: verifies DB connectivity."""
    await session.exec(text("SELECT 1"))  # type: ignore[call-overload]
    return {"status": "ready"}
