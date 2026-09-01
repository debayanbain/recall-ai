"""Aggregate v1 router."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    chat,
    integrations,
    public,
    search,
    spaces,
    vault,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(vault.router)
api_router.include_router(search.router)
api_router.include_router(chat.router)
api_router.include_router(spaces.router)
api_router.include_router(integrations.router)
api_router.include_router(public.router)
api_router.include_router(webhooks.router)
