"""Answering questions about a user's own vault.

The seam between the Telegram bot and the AI stack. Everything LangChain lives under
`app/ai/chat`; this module is what the dispatcher -- and any later `/ask` endpoint --
actually calls.

Three shapes of question, deliberately handled differently:

* **Pure time** ("what did I save this week?") -- a listing. No embedding, no vector
  search, no answer model. It is a `WHERE created_at >= …` and a formatted list, and
  spending three provider calls on it would be waste with a worse result.
* **Subject, with or without a period** ("any cooking videos from last week?") -- vector
  search, then a grounded answer.
* **Nothing found** -- a fixed sentence, written here. Handing an empty context to the
  model invites it to invent a memory, and pays for the privilege.

The return value is a `RecallAnswer`, never rendered text. The caller owns presentation:
this module has no idea whether it is answering into a Telegram chat, an HTTP response or
a future web view, and a service that returned Telegram HTML would quietly make every one
of those a Telegram client.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.ai.chat import chain, history, planner
from app.ai.chat.factory import chat_available
from app.ai.chat.planner import MemoryQuery, resolved_content_types
from app.ai.chat.retriever import VaultRetriever
from app.core.config import settings
from app.core.logging import get_logger
from app.models.vault import VaultItem
from app.repositories.vault import VaultRepository
from app.services.chat_engine.router import Intent, route

log = get_logger("recall.chat")

_LIST_LIMIT = 10

#: Presence-only link detection, for the router's `url` argument. The router does no URL
#: finding of its own -- that is the caller's job precisely because every surface carries
#: links differently, and this one has nothing but a string. It is deliberately NOT
#: `telegram.capture.first_url`: that reads a provider's entity offsets, and importing it
#: would make this module a client of the one surface its docstring promises it does not
#: know about. Nothing is ever fetched from the result; it only decides a branch.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _first_url(text: str) -> str | None:
    match = _URL_RE.search(text)
    return match.group(0) if match else None


@dataclass(slots=True)
class RecallAnswer:
    """Either prose grounded in retrieved memories, or a plain listing, or a failure.

    `items` and `text` are mutually exclusive in practice: a time-scoped question is
    answered by the list, everything else by the model.
    """

    text: str | None = None
    items: Sequence[VaultItem] = field(default_factory=list)
    total: int = 0
    failed: bool = False


class RecallChatService:
    def __init__(self, repo: VaultRepository) -> None:
        self.repo = repo

    async def respond(
        self, user_id: uuid.UUID, text: str, session_id: str
    ) -> RecallAnswer:
        """The one entry point for a plain-text message. Never writes anything.

        Two lanes, chosen by `chat_engine.router.route`, which costs no tokens:

        * **RECALL** -- a question about their vault, so the retrieval lane below.
        * **META and CHAT** -- a conversational reply with no retrieval at all. Both go
          to the same chain: `converse` already knows who it is (`BOT_IDENTITY`), so
          "what can you do" needs no separate branch here, only a guarantee that it does
          not reach `answer`.

        `route` replaced `planner.looks_like_question`, which routed on a trailing "?".
        That sent "who are you?" and "what is the capital of France?" down the retrieval
        lane, which spent an embedding and a vector scan to reply that nothing was saved
        about "you" -- a bot that looks broken every time someone talks to it. Phrases
        decide now; a question mark on its own does not.

        The split is by *shape*, not by asking a model to classify intent: a
        misclassification here is a wrong-looking reply either way, and a model call to
        decide whether to make a model call is a cost with no ceiling.
        """
        intent = route(text, url=_first_url(text))

        if intent is Intent.RECALL:
            return await self.answer(user_id, text, session_id)

        if intent is Intent.COMMAND or intent is Intent.CAPTURE:
            # Unreachable through the dispatcher, which claims commands, links and files
            # before any of this runs. Logged rather than raised: this executes inside a
            # Celery task serving a webhook Telegram redelivers on any non-2xx, so an
            # exception here is an infinite retry loop over a message we could have
            # simply talked back to. Falling through to `chat` loses nothing -- this
            # method cannot save anything in the first place.
            log.warning("recall_unexpected_intent", intent=intent.value)

        return await self.chat(text, session_id)

    async def chat(self, message: str, session_id: str) -> RecallAnswer:
        """Small talk. No vault access, so nothing to leak and nothing to inject."""
        past = await history.load(session_id)
        try:
            reply = await chain.converse(message, past)
        except Exception as exc:  # noqa: BLE001 - never surface a provider traceback
            log.warning("recall_converse_failed", error=type(exc).__name__)
            return RecallAnswer(failed=True)

        await history.append(session_id, message, reply)
        return RecallAnswer(text=reply.strip())

    async def answer(
        self, user_id: uuid.UUID, question: str, session_id: str
    ) -> RecallAnswer:
        plan = await planner.plan(question)
        created_after = _created_after(plan)

        if not plan.search_text:
            return await self._time_only(user_id, plan, created_after)

        retriever = VaultRetriever(
            repo=self.repo,
            user_id=user_id,
            limit=settings.TELEGRAM_RECALL_TOP_K,
            created_after=created_after,
            content_types=resolved_content_types(plan),
            category=plan.category,
        )
        documents = await retriever.ainvoke(plan.search_text)
        if not documents:
            return RecallAnswer(text=_nothing_found(plan))

        past = await history.load(session_id)
        try:
            reply = await chain.answer(question, documents, past)
        except Exception as exc:  # noqa: BLE001 - never surface a provider traceback
            log.warning("recall_answer_failed", error=type(exc).__name__)
            return RecallAnswer(failed=True)

        await history.append(session_id, question, reply)
        return RecallAnswer(text=reply.strip())

    async def _time_only(
        self, user_id: uuid.UUID, plan: MemoryQuery, created_after: datetime | None
    ) -> RecallAnswer:
        items, total = await self.repo.list_filtered(
            user_id,
            limit=_LIST_LIMIT,
            created_after=created_after,
            content_types=resolved_content_types(plan),
            category=plan.category,
        )
        if not items:
            return RecallAnswer(text=_nothing_found(plan))
        return RecallAnswer(items=items, total=total)


def _created_after(plan: MemoryQuery) -> datetime | None:
    if plan.days is None:
        return None
    return datetime.now(UTC) - timedelta(days=plan.days)


def _nothing_found(plan: MemoryQuery) -> str:
    subject = plan.search_text.strip()
    window = f" in the last {plan.days} days" if plan.days else ""
    if subject:
        return f"I couldn't find anything about “{subject}” in your vault{window}."
    return f"Nothing saved{window or ' yet'}."


def build_recall_responder(repo: VaultRepository) -> RecallChatService | None:
    """The retrieval half, or None when no chat model is configured.

    None is a real state, not an error: the bot still captures everything, and plain text
    is then always a note. A half-configured deployment must not answer questions with a
    provider error.
    """
    if not chat_available():
        return None
    return RecallChatService(repo)
