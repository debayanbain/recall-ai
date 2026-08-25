"""Provider callbacks.

Apify announces a finished run here instead of us holding a worker open for the length of
a crawl. Two rules make that safe:

* **The body is a signal, not data.** It names a run id; nothing from it is stored. The
  task then re-reads the run's real status and dataset from Apify with our own token, so
  a forged callback cannot inject content into anyone's vault.
* **The endpoint is not an open trigger.** A shared secret in the path gates it, compared
  in constant time, because otherwise anyone could make this deployment do unbounded
  background work by POSTing run ids at it.

It answers 200 as soon as the work is queued. Apify retries non-2xx, and doing the fetch
inline would put a multi-second call in the callback's critical path.
"""
from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.core.config import settings
from app.core.logging import get_logger
from app.queue.client import enqueue_finalize_run

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = get_logger("webhooks")


@router.post("/apify/{secret}", status_code=status.HTTP_202_ACCEPTED)
async def apify_webhook(secret: str, request: Request) -> dict[str, str]:
    configured = settings.APIFY_WEBHOOK_SECRET
    # An unset secret must not mean "allow everything".
    if not configured or not secrets.compare_digest(secret, configured):
        log.warning("apify_webhook_rejected")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    try:
        body: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON") from None

    resource = body.get("resource") or {}
    run_id = resource.get("id") if isinstance(resource, dict) else None
    if not run_id:
        # Apify also sends test pings with no resource; acknowledge rather than 4xx,
        # or the webhook is marked failing in their console.
        log.info("apify_webhook_no_run_id", event_type=str(body.get("eventType"))[:40])
        return {"status": "ignored"}

    await enqueue_finalize_run(str(run_id))
    log.info(
        "apify_webhook_queued",
        run_id=str(run_id),
        event_type=str(body.get("eventType"))[:40],
    )
    return {"status": "queued"}
