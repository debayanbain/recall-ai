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
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.ai.chat import agent, chain, history, planner, tools
from app.ai.chat.factory import chat_available
from app.ai.chat.planner import MemoryQuery, resolved_content_types
from app.ai.chat.retriever import to_document
from app.core.config import settings
from app.core.logging import get_logger
from app.core.scripts import script_of
from app.models.vault import VaultItem
from app.repositories.vault import VaultRepository
from app.services.chat_engine.cards import DETAIL_MAX_ITEMS, build_detail_card
from app.services.chat_engine.evidence import Evidence, EvidenceStatus, assess
from app.services.chat_engine.retrieval import MemoryFilters, MemoryRetriever
from app.services.chat_engine.router import wants_detail
from app.services.chat_engine.toolbox import MemoryToolbox
from app.services.chat_engine.types import (
    Delta,
    ErrorKind,
    ItemsEvent,
    StatusEvent,
    StreamEnd,
    StreamEvent,
)
from app.services.chat_engine.validation import StreamValidator, validate_answer

log = get_logger("recall.chat")

_LIST_LIMIT = 10

#: What to tell a waiting reader each tool is doing. A short phrase per tool rather than
#: the tool's own name, which is an implementation detail, and never its arguments.
_STAGES = {
    "SearchMemories": "searching your memories",
    "ListMemories": "looking through your saves",
    "GetMemory": "reading a memory",
}


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
        """A question about the vault. Two paths to the same guarantees.

        The tool lane lets the model run its own searches, which is what makes a
        find-then-read question answerable at all; the single-shot lane makes one planned
        search and answers from it. What does *not* change between them is everything
        that makes an answer trustworthy: the relevance gate runs on every search either
        way, the reply is validated against the evidence that produced it either way, and
        a turn that surfaced nothing gets the fixed sentence with no model call either
        way. The tool lane is the more capable path, not a laxer one -- and when it fails
        the single-shot path answers the same question rather than the user seeing an
        error.
        """
        if settings.RECALL_TOOLS_ENABLED:
            answered = await self._answer_with_tools(user_id, question, session_id)
            if answered is not None:
                return answered

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

    async def _answer_with_tools(
        self, user_id: uuid.UUID, question: str, session_id: str
    ) -> RecallAnswer | None:
        """The lane where the model chooses the searches. `None` means "fall back".

        The toolbox is built per question and thrown away with it: `allowed_ids` is the
        set of memories *this* answer may cite, and carrying it forward would let one
        answer cite evidence retrieved for a different question.
        """
        toolbox = MemoryToolbox(
            user_id, self.repo, top_k=settings.TELEGRAM_RECALL_TOP_K
        )
        past = await history.load(session_id)
        result = await tools.answer_with_tools(
            question,
            past,
            toolbox,
            max_calls=settings.RECALL_MAX_TOOL_CALLS,
            max_rounds=settings.RECALL_MAX_TOOL_ROUNDS,
        )
        if result is None:
            return None

        log.info(
            "recall_tool_answer",
            rounds=result.rounds,
            calls=result.calls,
            surfaced=len(toolbox.allowed_ids),
        )

        if toolbox.found_nothing:
            # Not a model's phrasing of "nothing found" but the fixed sentence, for the
            # same reason the other path uses one: an answer with no evidence behind it
            # is the exact input a model fills in from its own knowledge. Its own first
            # search term is the subject to echo back -- that is the model's extraction
            # of what was asked, which is what the planner would have produced.
            subject = toolbox.queries[0] if toolbox.queries else question
            return RecallAnswer(text=_nothing_found(MemoryQuery(search_text=subject)))

        checked = validate_answer(
            result.text,
            allowed_ids=toolbox.allowed_ids,
            allowed_urls=toolbox.allowed_urls,
            max_chars=settings.RECALL_ANSWER_MAX_CHARS,
        )
        if checked.removed:
            log.warning("recall_answer_corrected", removed=list(checked.removed[:5]))
        if checked.rejected:
            # An empty or unusable reply, having already spent the tool calls. Falling
            # back would spend the whole single-shot path to reach the same model, so
            # this is reported as the failure it is.
            return RecallAnswer(failed=True)

        await history.append(session_id, question, checked.text)
        return RecallAnswer(text=checked.text, memory_ids=toolbox.allowed_ids)

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


    # --- the same two lanes, delivered as they are written ------------------------------
    #
    # Streaming takes the single-shot path deliberately, not the tool lane. A tool round
    # is not a token: the model's first turns produce *calls*, not prose, so "stream the
    # answer" and "let the model search twice" are answers to different questions and
    # only one of them can be first. The bot, which nobody watches type, keeps the tool
    # lane; a page, where the wait is visible, gets the words as they arrive. Whichever
    # runs, the grounding and the output checks are identical.

    async def stream(
        self, user_id: uuid.UUID, question: str, session_id: str
    ) -> AsyncIterator[StreamEvent]:
        """A vault question, answered as it is written. Always ends with a `StreamEnd`.

        Two drivers, in preference order. The graph runs the *tools*, so the model can
        search, look at what came back and search again -- and streams its words while
        doing it. When it is off, or fails before saying anything, the single-shot path
        below answers instead: one planned search, one grounded answer. Both go through
        the same relevance gate and the same output validation; what differs is how many
        looks the model gets.
        """
        if settings.RECALL_TOOLS_ENABLED and settings.RECALL_AGENT_ENABLED:
            spoke = False
            async for event in self._stream_with_tools(user_id, question, session_id):
                spoke = spoke or isinstance(event, Delta)
                yield event
            if spoke:
                return
            # Nothing was said, so nothing has been shown to the reader and the question
            # can still be answered by the other driver. A failure *after* words have
            # been streamed is not recoverable this way -- repeating them is worse than
            # the shorter answer they already have.

        plan = await planner.plan(question)
        created_after = _created_after(plan)

        if not plan.search_text:
            answer = await self._time_only(user_id, plan, created_after)
            if answer.items:
                yield ItemsEvent(items=answer.items, total=answer.total)
            else:
                yield Delta(text=answer.text or "")
            yield StreamEnd()
            return

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
        evidence = assess(memories)
        log.info(
            "recall_evidence",
            status=evidence.status.value,
            retrieved=len(memories),
            kept=len(evidence.memories),
            best=round(evidence.best_score, 3),
            detail=detail,
            streamed=True,
        )
        if evidence.status is EvidenceStatus.no_evidence:
            # No model call, exactly as on the non-streaming path. There is nothing to
            # stream: the reply is one fixed sentence.
            yield Delta(text=_nothing_found(plan))
            yield StreamEnd()
            return

        documents = [
            to_document(item, body=build_detail_card(item) if detail else None)
            for item in evidence.items
        ]
        past = await history.load(session_id)
        checker = StreamValidator(
            allowed_ids=evidence.ids,
            allowed_urls=[item.source_url for item in evidence.items],
            max_chars=settings.RECALL_ANSWER_MAX_CHARS,
        )
        async for event in self._pump(
            chain.answer_stream(question, documents, past, _guidance(evidence)),
            checker,
            session_id=session_id,
            question=question,
            memory_ids=evidence.ids,
        ):
            yield event

    async def _stream_with_tools(
        self, user_id: uuid.UUID, question: str, session_id: str
    ) -> AsyncIterator[StreamEvent]:
        """The tool lane, streamed. Yields nothing at all when the driver is unavailable.

        Yielding nothing rather than an error event is what lets the caller retry on the
        other driver: an event the reader has already seen cannot be taken back, so this
        stays silent until it has something it is willing to stand behind.
        """
        toolbox = MemoryToolbox(
            user_id, self.repo, top_k=settings.TELEGRAM_RECALL_TOP_K
        )
        past = await history.load(session_id)
        # The validator is built before the first search, so its allowlist grows as the
        # toolbox surfaces memories. That ordering is the right way round: a citation can
        # only be checked against evidence that has already been retrieved, and the model
        # can only cite what it has already been shown.
        checker = StreamValidator(
            allowed_ids=toolbox.allowed_ids,
            allowed_urls=toolbox.allowed_urls,
            max_chars=settings.RECALL_ANSWER_MAX_CHARS,
        )
        spoken: list[str] = []
        async for event in agent.stream_with_tools(
            question, past, toolbox, max_rounds=settings.RECALL_MAX_TOOL_ROUNDS
        ):
            if isinstance(event, agent.AgentToolCall):
                yield StatusEvent(stage=_STAGES.get(event.name, "working"))
                continue
            if isinstance(event, agent.AgentDelta):
                # The allowlist is re-read on every fragment: a search that ran between
                # two deltas has added to it, and a validator holding a snapshot from
                # before the search would strip a citation of a memory the model was
                # legitimately shown.
                checker.allowed_ids = list(toolbox.allowed_ids)
                checker.allowed_urls = list(toolbox.allowed_urls)
                ready = checker.feed(event.text)
                if ready:
                    spoken.append(ready)
                    yield Delta(text=ready)
                continue

            tail = checker.finish()
            if tail:
                spoken.append(tail)
                yield Delta(text=tail)
            if event.failed and not spoken:
                # Silent on purpose. The caller answers by the other route.
                return
            if checker.removed:
                log.warning("recall_answer_corrected", removed=list(checker.removed[:5]))
            log.info(
                "recall_agent_answer", calls=event.calls, surfaced=len(toolbox.allowed_ids)
            )
            if checker.rejected:
                yield StreamEnd(error=ErrorKind.provider_failure)
                return
            await history.append(session_id, question, "".join(spoken).strip())
            yield StreamEnd(
                memory_ids=toolbox.allowed_ids, corrected=bool(checker.removed)
            )

    async def stream_chat(self, message: str, session_id: str) -> AsyncIterator[StreamEvent]:
        """Small talk, as it is written. No vault access, so no evidence and no URLs.

        `allowed_urls=()` is not an oversight: this lane is handed no memories, so every
        link it could emit is unsupported by construction.
        """
        past = await history.load(session_id)
        checker = StreamValidator(
            allowed_ids=(),
            allowed_urls=(),
            max_chars=settings.CHAT_REPLY_MAX_CHARS,
        )
        async for event in self._pump(
            chain.converse_stream(message, past),
            checker,
            session_id=session_id,
            question=message,
        ):
            yield event

    async def _pump(
        self,
        chunks: AsyncIterator[str],
        checker: StreamValidator,
        *,
        session_id: str,
        question: str,
        memory_ids: tuple[str, ...] = (),
    ) -> AsyncIterator[StreamEvent]:
        """Drive one provider stream through the validator and out as events.

        Two things are worth knowing about the ending. **The checked text is what reaches
        history**, exactly as on the non-streaming path -- history is replayed into the
        next prompt, so storing the raw stream would let a stripped fabrication come back
        as context and be built on. And a provider that dies mid-stream still ends with a
        `StreamEnd`, carrying the error: a stream that simply stops leaves a reader
        watching a cursor that will never move.
        """
        spoken: list[str] = []
        try:
            async for chunk in chunks:
                ready = checker.feed(chunk)
                if ready:
                    spoken.append(ready)
                    yield Delta(text=ready)
            tail = checker.finish()
            if tail:
                spoken.append(tail)
                yield Delta(text=tail)
        except Exception as exc:  # noqa: BLE001 - never surface a provider traceback
            log.warning("recall_stream_failed", error=type(exc).__name__)
            yield StreamEnd(error=ErrorKind.provider_failure)
            return

        if checker.removed:
            log.warning("recall_answer_corrected", removed=list(checker.removed[:5]))
        if checker.rejected:
            yield StreamEnd(error=ErrorKind.provider_failure)
            return

        await history.append(session_id, question, "".join(spoken).strip())
        yield StreamEnd(memory_ids=memory_ids, corrected=bool(checker.removed))


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


@dataclass(frozen=True)
class _NoMatchPhrasing:
    """The four strings needed to say "nothing found" in one language."""

    #: Takes {subject} and {window}; {window} may be empty.
    with_subject: str
    #: Takes {window}, which is non-empty here.
    without_subject: str
    #: No window at all -- the "yet" form, where English says "Nothing saved yet."
    without_subject_ever: str
    #: Takes {days}. Rendered first, then interpolated into the two above.
    window: str


#: This reply is produced with **no model call** -- an empty context is the one input the
#: answer prompt has no honest response to, and paying a provider to say "nothing" is the
#: wrong trade. That is exactly why it needs a table: there is nothing in the loop that
#: could translate it.
#:
#: Keyed by *script*, not language, because script is all `script_of` can honestly tell
#: us. That has a consequence worth stating: `devanagari` covers Hindi, Marathi and
#: Nepali, and a Marathi speaker gets the Hindi sentence. Better than English for them,
#: and not something character counting can improve on.
#:
#: **Deliberately short.** Every entry is a sentence a real person reads at the moment
#: their search failed, and a machine-translated one that reads as broken is worse than
#: plain English -- it makes the product look careless in exactly the language it was
#: trying to respect. Anything not listed falls back to English on purpose. Adding a
#: language is one entry, and it should be written by someone who speaks it.
_NO_MATCH: dict[str, _NoMatchPhrasing] = {
    "bengali": _NoMatchPhrasing(
        with_subject="{window}আপনার ভল্টে “{subject}” সম্পর্কে কিছু পাইনি।",
        without_subject="{window}কিছু সেভ করা হয়নি।",
        without_subject_ever="এখনও কিছু সেভ করা হয়নি।",
        window="গত {days} দিনে ",
    ),
    "devanagari": _NoMatchPhrasing(
        with_subject="{window}आपके वॉल्ट में “{subject}” के बारे में कुछ नहीं मिला।",
        without_subject="{window}कुछ भी सेव नहीं किया गया।",
        without_subject_ever="अभी तक कुछ भी सेव नहीं किया गया।",
        window="पिछले {days} दिनों में ",
    ),
}

_NO_MATCH_ENGLISH = _NoMatchPhrasing(
    with_subject="I couldn't find anything about “{subject}” in your vault{window}.",
    without_subject="Nothing saved{window}.",
    without_subject_ever="Nothing saved yet.",
    window=" in the last {days} days",
)


def _nothing_found(plan: MemoryQuery) -> str:
    """The fixed reply for a search that matched nothing, in the asker's script.

    The subject is echoed back verbatim -- it is the user's own words, and translating it
    would hand them back a term they never searched for.
    """
    subject = plan.search_text.strip()
    phrasing = _NO_MATCH.get(script_of(subject) or "", _NO_MATCH_ENGLISH)
    window = phrasing.window.format(days=plan.days) if plan.days else ""
    if subject:
        return phrasing.with_subject.format(subject=subject, window=window)
    if window:
        return phrasing.without_subject.format(window=window)
    return phrasing.without_subject_ever


def build_recall_responder(repo: VaultRepository) -> RecallChatService | None:
    """The retrieval half, or None when no chat model is configured.

    None is a real state, not an error: the bot still captures everything, and plain text
    is then always a note. A half-configured deployment must not answer questions with a
    provider error.
    """
    if not chat_available():
        return None
    return RecallChatService(repo)
