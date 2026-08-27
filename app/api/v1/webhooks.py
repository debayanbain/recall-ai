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

import json
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.core.config import settings
from app.core.logging import get_logger
from app.queue.client import enqueue_finalize_run, enqueue_telegram_update

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = get_logger("webhooks")

# A Telegram update is small -- a message with a caption is a few KB. The cap exists
# so an unauthenticated POST cannot make this process buffer an arbitrary body before
# the secret is even checked against it.
_MAX_TELEGRAM_BODY = 1_000_000


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


@router.post("/telegram/{secret}", status_code=status.HTTP_202_ACCEPTED)
async def telegram_webhook(secret: str, request: Request) -> dict[str, str]:
    """Receive one Telegram update, queue it, answer immediately.

    Two independent checks, both constant-time: the shared secret in the path, and the
    same secret echoed by Telegram in `X-Telegram-Bot-Api-Secret-Token`. The header is
    what makes a leaked URL -- from a proxy log, a screenshot, a browser history --
    insufficient on its own.

    Everything after this returns 2xx. Telegram retries any non-2xx with the same update,
    so a 500 on one malformed message becomes an infinite redelivery loop.
    """
    configured = settings.TELEGRAM_WEBHOOK_SECRET
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    if (
        not configured
        or not secrets.compare_digest(secret, configured)
        or not secrets.compare_digest(header, configured)
    ):
        log.warning("telegram_webhook_rejected")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    raw = await request.body()
    if len(raw) > _MAX_TELEGRAM_BODY:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Update too large")
    try:
        body: dict[str, Any] = json.loads(raw)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON") from None

    # Only `message` updates are subscribed in setWebhook, but Telegram can still deliver
    # others after a settings change; acknowledge them rather than retrying forever.
    if not isinstance(body.get("message"), dict):
        log.info("telegram_webhook_ignored", update_id=str(body.get("update_id"))[:20])
        return {"status": "ignored"}

    await enqueue_telegram_update(body)
    log.info("telegram_webhook_queued", update_id=str(body.get("update_id"))[:20])
    return {"status": "queued"}
