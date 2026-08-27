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

log = get_logger("recall.chat")

_LIST_LIMIT = 10


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

        Two lanes, split by `planner.looks_like_question`, which costs no tokens:

        * **A question about their vault** -- the retrieval lane below.
        * **Anything else** ("hi", "what can you do") -- a conversational reply with no
          retrieval at all. Routing this into `answer` instead would run an embedding
          and a vector search over a greeting, then report that nothing was saved about
          "hi", which reads as a broken bot rather than a quiet one.

        The split is by *shape*, not by asking a model to classify intent: a
        misclassification here is a wrong-looking reply either way, and a model call to
        decide whether to make a model call is a cost with no ceiling.
        """
        if planner.looks_like_question(text):
            return await self.answer(user_id, text, session_id)
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
