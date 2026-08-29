"""The "…is typing" line, kept alive for as long as the work actually takes.

Without this the bot is silent from the moment a message is sent until the reply lands,
and the reply is not fast: a recall question is an embedding, a planner call, a vector
scan and an answer call, and a capture is a download plus an upload plus a database
write. Several seconds of nothing looks exactly like a bot that did not receive the
message, so people send it again -- which costs another run of the same work and, on the
capture path, produces a duplicate the user then has to clean up.

Telegram's own mechanism is `sendChatAction`, and it has one property that decides the
shape of this module: **the indicator expires after about five seconds** and there is no
"stop typing" call. One action at the start therefore covers only the fastest replies and
then goes quiet in the middle of the wait -- which is worse than never showing it, because
it reads as the bot having given up. So it is re-sent on a timer for as long as the work
runs, from a background task cancelled the moment the caller's block exits. Sending the
reply clears the indicator by itself; nothing has to switch it off.

It is applied in **two** places, because the wait has two halves. The webhook fires a
single action the moment the update is acknowledged (`send_typing_once`), which covers the
hop through Redis to a worker -- otherwise the first dot appears only once something
dequeues the update, and on a busy or cold worker that is the most visible part of the
wait. The worker then runs the repeating loop for as long as the work actually takes. The
two overlap on purpose: one action lasts about five seconds, which is more than enough to
carry the queue hop.

Three things it deliberately does not do:

* **It never fails the work.** `send_chat_action` already swallows API errors, and the
  loop swallows the rest: a cosmetic indicator must not be able to turn a successful
  capture into a retry.
* **It stops on its own.** `MAX_SECONDS` bounds the loop, so a task that hangs leaves a
  chat that has stopped typing rather than one typing forever.
* **It says nothing about the sender.** The action is addressed to the chat the update
  came from, and nothing else about that chat is read -- typing at someone is not a
  disclosure, which is why this may run before the `telegram_accounts` lookup that
  decides whether the sender is anyone at all.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from app.core.logging import get_logger
from app.services.telegram.client import TelegramClient

log = get_logger("telegram")

#: Telegram drops the indicator after ~5s. Re-sent a little sooner so the gap between
#: one action expiring and the next arriving never becomes visible.
REFRESH_SECONDS = 4.0

#: A ceiling on how long the bot will claim to be typing. Past this the honest thing is
#: to stop: the work has either hung or is long enough that the user has put the phone
#: down, and a chat that types for five minutes is its own kind of broken.
MAX_SECONDS = 120.0


def chat_id_of(update: dict[str, Any]) -> str | None:
    """The private chat an update came from, or None.

    Read straight off the payload rather than from the parsed message, because the
    indicator has to start *before* the work does -- including before parsing, and long
    before the sender has been resolved to an account.

    Group chats are excluded here as they are everywhere else in this surface: the bot
    does not act in a room, so it must not look like it is about to.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "private":
        return None
    chat_id = chat.get("id")
    return str(chat_id) if chat_id is not None else None


async def _keep_typing(client: Any, chat_id: str, action: str) -> None:
    """Re-send the chat action until cancelled, the deadline passes, or it stops working."""
    deadline = time.monotonic() + MAX_SECONDS
    try:
        while True:
            await client.send_chat_action(chat_id, action)
            if time.monotonic() + REFRESH_SECONDS >= deadline:
                return
            await asyncio.sleep(REFRESH_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a typing dot must never fail the work
        log.debug("telegram_typing_loop_failed", error=type(exc).__name__)


@asynccontextmanager
async def typing_action(
    client: Any, chat_id: str | None, action: str = "typing"
) -> AsyncIterator[None]:
    """Show "typing…" in `chat_id` for the duration of the block.

    `client` is a `TelegramClient` inside its own `async with`; it is typed loosely so
    this module does not have to be imported by everything that fakes one in a test.
    A `chat_id` of None is a no-op, which is what makes the caller a plain `async with`
    rather than a branch.
    """
    if not chat_id:
        yield
        return

    task = asyncio.create_task(_keep_typing(client, chat_id, action))
    try:
        yield
    finally:
        task.cancel()
        # Awaited rather than left dangling: an un-awaited cancelled task logs
        # "Task exception was never retrieved" at interpreter shutdown, and the worker
        # runs one event loop per Celery task, so shutdown is every few seconds.
        with suppress(asyncio.CancelledError, Exception):
            await task


async def send_typing_once(chat_id: str) -> None:
    """One chat action, from a process that holds no client of its own.

    This is the API's half, run as a FastAPI background task so it happens *after* the
    202 rather than inside it: the webhook's whole contract is to acknowledge fast, and
    Telegram redelivers anything it does not get an answer to. It opens and closes its
    own client for the same reason `notices.notify_degraded` does -- the request path has
    no long-lived Bot API client to borrow.

    One action, not a loop. It buys the ~5 seconds it takes an update to reach a worker,
    and the worker's own `typing_action` takes over from there; a loop here would keep an
    HTTP client alive in the web process for the length of somebody else's work.

    Never raises, and never rate-limits itself: it is one call per inbound message, the
    same ratio as the reply that follows it, and a Telegram 429 on a chat action is a
    silently dropped dot rather than a failure.
    """
    if not chat_id:
        return
    try:
        async with TelegramClient() as client:
            await client.send_chat_action(chat_id)
    except Exception as exc:  # noqa: BLE001 - a typing dot must never fail the webhook
        log.debug("telegram_typing_once_failed", error=type(exc).__name__)
