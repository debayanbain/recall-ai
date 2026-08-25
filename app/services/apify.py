"""Apify run inspection.

Used after a webhook fires. The webhook body is treated as an untrusted *signal* — it
says only "run X finished" — and the authoritative status and dataset id are read back
here with our own token. That way a forged callback cannot inject content into a vault.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings

_RUNS = "https://api.apify.com/v2/actor-runs"


async def get_run(run_id: str) -> dict[str, Any]:
    """Fetch a run's authoritative record."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{_RUNS}/{run_id}",
            headers={"Authorization": f"Bearer {settings.APIFY_TOKEN}"},
        )
        resp.raise_for_status()
    data = resp.json().get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Apify returned an unexpected run payload")
    return data
