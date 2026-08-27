"""Register, inspect or remove the Telegram webhook.

Telegram has to be told where to deliver updates, and the URL contains a secret, so this
is a script rather than a line in the README that ends up pasted into a shell history.
Everything comes from `settings` -- `app/core/config.py` is the only place that reads the
environment, and that holds for scripts too.

    make telegram-webhook          # register PUBLIC_BASE_URL as the target
    make telegram-webhook-info     # what Telegram currently thinks
    make telegram-webhook-delete   # stop delivery

Nothing here prints the bot token or the webhook secret. The registered URL contains the
secret, so `info` reports only the host and the path shape.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import settings
from app.services.telegram.client import TelegramApiError, TelegramClient


def _webhook_url() -> str:
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}{settings.API_V1_PREFIX}/webhooks/telegram/{settings.TELEGRAM_WEBHOOK_SECRET}"


def _redacted(url: str) -> str:
    """Show enough to confirm the right deployment, never the secret itself."""
    if not url:
        return "(none)"
    head, _, _ = url.rpartition("/")
    return f"{head}/<secret>"


async def _register() -> int:
    url = _webhook_url()
    async with TelegramClient() as client:
        await client.set_webhook(url, settings.TELEGRAM_WEBHOOK_SECRET)
    print(f"registered  {_redacted(url)}")
    print("            secret_token set; the API checks it on every delivery")
    return 0


async def _info() -> int:
    async with TelegramClient() as client:
        result = await client.get_webhook_info()
    print(f"url                  {_redacted(str(result.get('url') or ''))}")
    print(f"pending updates      {result.get('pending_update_count')}")
    if result.get("last_error_message"):
        print(f"last error           {result['last_error_message']}")
        print(f"last error at        {result.get('last_error_date')}")
    return 0


async def _delete() -> int:
    async with TelegramClient() as client:
        await client.delete_webhook()
    print("deleted     Telegram will stop delivering updates")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", nargs="?", default="register", choices=("register", "info", "delete")
    )
    action = parser.parse_args().action

    if not settings.telegram_enabled:
        print(
            "Telegram is not configured. Set TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME "
            "and TELEGRAM_WEBHOOK_SECRET in .env.",
            file=sys.stderr,
        )
        return 1
    if action == "register" and not settings.PUBLIC_BASE_URL.startswith("https://"):
        print(
            "PUBLIC_BASE_URL must be an https:// URL Telegram can reach. In development "
            "that is the tunnel -- see `make dev-tunnel`.",
            file=sys.stderr,
        )
        return 1

    runner = {"register": _register, "info": _info, "delete": _delete}[action]
    try:
        return asyncio.run(runner())
    except TelegramApiError as exc:
        # The message is Telegram's own text; it carries no credential.
        print(f"Telegram refused the call: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
