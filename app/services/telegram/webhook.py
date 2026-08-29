"""Where Telegram is told to deliver, and keeping that true after a restart.

Telegram remembers one URL per bot, set once by an API call. Nothing re-checks it, so
the day the public URL changes -- a new tunnel, a redeploy on a different host -- the bot
goes silent in the worst possible way: Telegram keeps accepting messages, answers the
user nothing, and records the failure only in `getWebhookInfo`, which nobody reads. The
last incident looked exactly like that: `last error: Wrong response from the webhook:
404 Not Found`, with the sender seeing no reply and no error.

So registration is reconciled at boot rather than performed by hand. `ensure_registered`
compares what Telegram holds against what this deployment *is* and corrects it only when
they differ -- an unconditional `setWebhook` on every start would drop pending updates on
every reload.

It never raises. A bot that cannot reach Telegram at boot must not stop the API from
serving HTTP; the failure is logged and the next start retries.
"""
from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.services.telegram.client import TelegramClient

log = get_logger("telegram")


def webhook_url() -> str:
    """The URL this deployment expects Telegram to deliver to.

    The secret is in the path *and* echoed in a header on every delivery, so this string
    is a credential: it is never logged whole. `redacted` is what goes in a log line.
    """
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return (
        f"{base}{settings.API_V1_PREFIX}/webhooks/telegram/"
        f"{settings.TELEGRAM_WEBHOOK_SECRET}"
    )


def redacted(url: str) -> str:
    """Enough to identify the deployment, never enough to deliver to it."""
    if not url:
        return "(none)"
    head, _, _ = url.rpartition("/")
    return f"{head}/<secret>"


def can_register() -> bool:
    """Whether registration is even possible here.

    `https://` is required by Telegram, which is why local development needs a tunnel;
    a plain-http base is a misconfiguration to report, not something to send.
    """
    return bool(
        settings.telegram_enabled
        and settings.TELEGRAM_WEBHOOK_SECRET
        and settings.PUBLIC_BASE_URL.startswith("https://")
    )


async def ensure_registered() -> str:
    """Reconcile Telegram's idea of the delivery URL with this deployment's.

    Returns a short status for logging: what happened, never the URL.
    """
    if not can_register():
        return "skipped"

    wanted = webhook_url()
    async with TelegramClient() as client:
        info = await client.get_webhook_info()
        current = str(info.get("url") or "")

        # Telegram reports the last delivery failure here and nowhere else. Surfacing it
        # at boot is the difference between "the bot is broken" and a one-line answer.
        if info.get("last_error_message"):
            log.warning(
                "telegram_webhook_last_error",
                error=str(info["last_error_message"])[:200],
                pending=info.get("pending_update_count"),
            )

        if current == wanted:
            log.info(
                "telegram_webhook_current",
                url=redacted(current),
                pending=info.get("pending_update_count"),
            )
            return "unchanged"

        await client.set_webhook(wanted, settings.TELEGRAM_WEBHOOK_SECRET)

    log.info(
        "telegram_webhook_registered",
        url=redacted(wanted),
        previous=redacted(current),
    )
    return "registered"


async def ensure_registered_quietly() -> str:
    """`ensure_registered`, but a failure is a log line rather than a dead API."""
    try:
        return await ensure_registered()
    except Exception as exc:  # noqa: BLE001 - the API must boot without Telegram
        log.warning("telegram_webhook_registration_failed", error=type(exc).__name__)
        return "failed"
