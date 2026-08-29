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

"Nothing found" now means *nothing relevant enough*, not merely zero rows. A vector
search always returns its k nearest items however far away they are, so the relevance
floor in `chat_engine/evidence.py` is what turns "eight unrelated memories" into the same
fixed sentence an empty result gets -- and it is applied before the model is called, not
described to it afterwards. What comes back from the model is then checked against the
evidence it was given (`chat_engine/validation.py`): a citation naming a memory that was
never supplied, or a URL that appears in no block, is removed on the way out. The prompt
asks; these two enforce.

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
from app.ai.chat.retriever import to_document
from app.core.config import settings
from app.core.logging import get_logger
from app.models.vault import VaultItem
from app.repositories.vault import VaultRepository
from app.services.chat_engine.cards import DETAIL_MAX_ITEMS, build_detail_card
from app.services.chat_engine.evidence import Evidence, EvidenceStatus, assess
from app.services.chat_engine.retrieval import MemoryFilters, MemoryRetriever
from app.services.chat_engine.router import wants_detail
from app.services.chat_engine.validation import validate_answer

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
    #: The memories the answer was generated from, by short id, in relevance order.
    #: Nothing renders these today -- they exist so a wrong answer can be traced back to
    #: the exact evidence that produced it, and so a later surface can cite sources
    #: without the answer path having to be rebuilt to remember what it read.
    memory_ids: tuple[str, ...] = ()


class RecallChatService:
    def __init__(self, repo: VaultRepository) -> None:
        self.repo = repo
        self.memories = MemoryRetriever(repo)

    async def chat(self, message: str, session_id: str) -> RecallAnswer:
        """Small talk. No vault access, so nothing to leak and nothing to inject.

        The reply is bounded on the way out for the same reason the recall answer is,
        and harder. This lane has **no evidence at all** behind it, so every URL in its
        output is unsupported by construction -- there is no block a link could have come
        from -- and a reply that has run to essay length is a reply that stopped being
        about this product. Neither check can be argued with by a message, which is what
        makes it worth having behind a prompt that can be.
        """
        past = await history.load(session_id)
        try:
            reply = await chain.converse(message, past)
        except Exception as exc:  # noqa: BLE001 - never surface a provider traceback
            log.warning("recall_converse_failed", error=type(exc).__name__)
            return RecallAnswer(failed=True)

        checked = validate_answer(
            reply,
            allowed_ids=(),
            allowed_urls=(),
            max_chars=settings.CHAT_REPLY_MAX_CHARS,
        )
        if checked.removed:
            log.warning("recall_chat_corrected", removed=list(checked.removed[:5]))
        if checked.rejected:
            return RecallAnswer(failed=True)

        await history.append(session_id, message, checked.text)
        return RecallAnswer(text=checked.text)

    async def answer(
        self, user_id: uuid.UUID, question: str, session_id: str
    ) -> RecallAnswer:
        plan = await planner.plan(question)
        created_after = _created_after(plan)

        if not plan.search_text:
            return await self._time_only(user_id, plan, created_after)

        # A question asking what a memory *said* is answered from fewer memories, read
        # more closely. Everything else gets cards, which is the default for a reason:
        # the body is the expensive half and it identifies nothing.
        detail = wants_detail(question)
        memories = await self.memories.recall(
            user_id,
            plan.search_text,
            MemoryFilters(
                created_after=created_after,
                content_types=resolved_content_types(plan),
                category=plan.category,
            ),
            limit=DETAIL_MAX_ITEMS if detail else settings.TELEGRAM_RECALL_TOP_K,
        )

        # Top-k is not truth. What the search returned is filtered on relevance here,
        # before a prompt exists, so a question with no good match costs nothing and
        # cannot be answered from a memory that merely happened to be nearest.
        evidence = assess(memories)
        log.info(
            "recall_evidence",
            status=evidence.status.value,
            retrieved=len(memories),
            kept=len(evidence.memories),
            best=round(evidence.best_score, 3),
            detail=detail,
        )
        if evidence.status is EvidenceStatus.no_evidence:
            # No model call at all. An empty -- or merely irrelevant -- context invites
            # the model to invent a memory and pays for the privilege.
            return RecallAnswer(text=_nothing_found(plan))

        documents = [
            to_document(item, body=build_detail_card(item) if detail else None)
            for item in evidence.items
        ]

        past = await history.load(session_id)
        try:
            reply = await chain.answer(question, documents, past, _guidance(evidence))
        except Exception as exc:  # noqa: BLE001 - never surface a provider traceback
            log.warning("recall_answer_failed", error=type(exc).__name__)
            return RecallAnswer(failed=True)

        checked = validate_answer(
            reply,
            allowed_ids=evidence.ids,
            allowed_urls=[item.source_url for item in evidence.items],
            max_chars=settings.RECALL_ANSWER_MAX_CHARS,
        )
        if checked.removed:
            # Worth a warning rather than a debug line: an unknown id or an unknown URL
            # is the model having produced a reference to a memory that was never in
            # front of it, which is the failure this whole path exists to prevent.
            log.warning("recall_answer_corrected", removed=list(checked.removed[:5]))
        if checked.rejected:
            return RecallAnswer(failed=True)

        # The *checked* text goes into history, never the raw reply. History is replayed
        # into the next prompt, so storing an answer with a fabricated citation in it
        # would let the fabrication come back as context and be built on.
        await history.append(session_id, question, checked.text)
        return RecallAnswer(text=checked.text, memory_ids=evidence.ids)

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


def _guidance(evidence: Evidence) -> str:
    """How far the answer may go with what was retrieved.

    Two states reach the model, not one: a weak-but-present match is answered honestly
    as a weak match rather than either silently upgraded to an answer or dropped as
    nothing. Which is which is the score's decision, made in `evidence.assess`.
    """
    if evidence.status is EvidenceStatus.supported:
        return chain.GUIDANCE_SUPPORTED
    return chain.GUIDANCE_WEAK


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
