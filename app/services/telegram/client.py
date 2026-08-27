"""Thin Telegram Bot API client.

Deliberately small: only the six calls the bot actually makes, each returning parsed
JSON or raising `TelegramApiError`. There is no third-party bot framework because the
update loop here is one function -- a framework would bring its own dispatcher, its own
event loop assumptions and its own webhook server, none of which survive contact with
Celery's prefork workers.

**Nothing in this module may log a request URL.** The bot token sits in the path
(`/bot<token>/sendMessage`), and `log_sink` redacts by key, not by value -- a URL logged
as a string would put the token on disk in the clear, and a log file gets copied, pasted
into an issue and archived.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("telegram")

_API_HOST = "https://api.telegram.org"
_TIMEOUT = 20.0
# Telegram file paths look like "photos/file_12.jpg" -- a relative path under the bot's
# own storage. Anything else is refused rather than pasted into a URL.
_FILE_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")


class TelegramApiError(RuntimeError):
    """A Bot API call failed. Safe to log; never rendered to a user verbatim."""


class TelegramClient:
    """One client per unit of work. Callers own the lifetime via `async with`."""

    def __init__(self, token: str | None = None, *, timeout: float = _TIMEOUT) -> None:
        self._token = token or settings.TELEGRAM_BOT_TOKEN
        if not self._token:
            raise TelegramApiError("TELEGRAM_BOT_TOKEN is not configured")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> TelegramClient:
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise TelegramApiError("TelegramClient used outside an `async with` block")
        return self._client

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{_API_HOST}/bot{self._token}/{method}"
        try:
            response = await self._http.post(url, json=payload)
        except httpx.HTTPError as exc:
            # str(exc) can contain the request URL, and the URL contains the token.
            raise TelegramApiError(f"{method} failed: {type(exc).__name__}") from None

        if response.status_code >= 400:
            raise TelegramApiError(f"{method} returned HTTP {response.status_code}")
        body: dict[str, Any] = response.json()
        if not body.get("ok"):
            # `description` is Telegram's own text and carries no credential.
            raise TelegramApiError(f"{method}: {str(body.get('description'))[:200]}")
        result = body.get("result")
        return result if isinstance(result, dict) else {"result": result}

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        disable_preview: bool = True,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send HTML-formatted text.

        HTML rather than MarkdownV2 on purpose: MarkdownV2 requires escaping 18
        characters including `.` and `-`, so a single unescaped title turns into a 400
        and the user gets nothing at all. HTML needs only `& < >`, which
        `formatting.escape` handles.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": disable_preview},
        }
        # Omitted rather than sent as null: Telegram treats a present-but-empty
        # `reply_markup` as a keyboard to render, and an empty one is a 400.
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", payload)

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        """Best-effort typing indicator. A failure here must never fail the capture."""
        try:
            await self._call("sendChatAction", {"chat_id": chat_id, "action": action})
        except TelegramApiError:
            log.debug("telegram_chat_action_failed", chat_id_present=bool(chat_id))

    async def get_file(self, file_id: str) -> dict[str, Any]:
        return await self._call("getFile", {"file_id": file_id})

    async def download_file(self, file_path: str, *, max_bytes: int) -> bytes:
        """Fetch a file from the bot's own storage, capped at `max_bytes`.

        `file_path` comes back from `getFile`, i.e. from Telegram rather than from the
        user -- but it is interpolated into a URL, so it is validated anyway. A path
        containing `..`, a leading `/` or a scheme would let a compromised or spoofed
        response point this fetch somewhere else entirely.
        """
        if not _FILE_PATH_RE.match(file_path) or ".." in file_path:
            raise TelegramApiError("Refusing to download an unexpected file path")

        safe_path = "/".join(quote(part, safe="") for part in file_path.split("/"))
        url = f"{_API_HOST}/file/bot{self._token}/{safe_path}"

        chunks: list[bytes] = []
        total = 0
        try:
            async with self._http.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise TelegramApiError(f"file download returned {response.status_code}")
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise TelegramApiError("file exceeds the size limit")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise TelegramApiError(f"file download failed: {type(exc).__name__}") from None
        return b"".join(chunks)

    async def set_webhook(self, url: str, secret_token: str) -> dict[str, Any]:
        """Register the webhook. `drop_pending_updates` clears a backlog from a prior run."""
        return await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "drop_pending_updates": True,
                "allowed_updates": ["message"],
            },
        )

    async def get_webhook_info(self) -> dict[str, Any]:
        return await self._call("getWebhookInfo", {})

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> dict[str, Any]:
        return await self._call(
            "deleteWebhook", {"drop_pending_updates": drop_pending_updates}
        )
