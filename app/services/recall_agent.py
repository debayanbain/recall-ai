"""The agent lane: one turn, bounded, checked, and able to fall back to the old one.

A subclass of `RecallChatService` rather than a replacement for it, and the reason is the
degradation ladder in the design doc: rung two is "the older, better-tested path answers
the same question", and as a subclass that rung is literally `super()`. Nothing has to be
re-wired to fall back, and nothing can drift between the two.

    1. this class          -- the graph, its tools, the snapshot, the budget
    2. super()             -- the single-shot planner path, unchanged
    3. no chat model       -- the deterministic lanes; `ChatEngine` answers that itself
    4. no Redis            -- no history, no proposals; still answers

The fall to rung two happens **only before a word has been shown**. After that the turn is
final: repeating sentences a reader has already seen is worse than the shorter answer they
have, so a failure that late is reported as an ending rather than as a retry.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator

from app.ai.chat import history
from app.ai.chat.harness import graph
from app.ai.prompts import CAPABILITY_CARD
from app.core.config import settings
from app.core.logging import get_logger
from app.repositories.vault import VaultRepository
from app.services.chat_engine import budget as budgets
from app.services.chat_engine.context import Snapshot, load_snapshot, render_snapshot
from app.services.chat_engine.guard import guard
from app.services.chat_engine.proposals import ProposalStore, RedisProposalStore
from app.services.chat_engine.toolbox import ConnectionReader, MemoryToolbox
from app.services.chat_engine.trace import AgentTrace
from app.services.chat_engine.types import (
    Delta,
    ErrorKind,
    ProposalBlock,
    ProposalEvent,
    QuestionBlock,
    QuestionEvent,
    StatusEvent,
    StreamEnd,
    StreamEvent,
)
from app.services.chat_engine.validation import StreamValidator
from app.services.recall_chat import RecallAnswer, RecallChatService

log = get_logger("recall.agent")

#: What a surface is told while a tool runs. Stage names, never arguments -- the same
#: rule the older lane's `_STAGES` follows, for the same reason.
_STAGES = {
    # `QueryMemories` was missing here, so the agent lane's main read fell through to the
    # generic "working" -- the one tool most likely to be running while somebody waits.
    "QueryMemories": "searching your memories",
    "GetConnections": "following the connections",
    "SearchMemories": "searching your memories",
    "ListMemories": "looking through your saves",
    "GetMemory": "reading a memory",
    "GetCaptureStatus": "checking that save",
    "AskUser": "working",
    "ProposeNote": "getting that ready",
    "ProposeRetry": "getting that ready",
    "ProposeConnect": "getting that ready",
    "FinalAnswer": "writing the answer",
}


class RecallAgentService(RecallChatService):
    """`RecallChatService` with a lane in front of it that chooses its own steps."""

    def __init__(
        self,
        repo: VaultRepository,
        store: ProposalStore | None = None,
        connections: ConnectionReader | None = None,
    ) -> None:
        super().__init__(repo)
        #: Where a proposed write is parked until somebody taps it. `None` means the
        #: propose tools are simply not bound this turn, and the prompt already tells
        #: the model what to say when it cannot offer to write.
        self.store = store
        #: Reads the edges between this person's memories. `None` means `GetConnections`
        #: is not bound and the `connections` projection returns nothing -- a deployment
        #: without it answers exactly as it did before the tool existed.
        self.connections = connections

    async def agent(
        self, user_id: uuid.UUID, question: str, session_id: str, *, store: bool = True
    ) -> RecallAnswer:
        """One agent turn, delivered whole. Falls back rather than failing."""
        parts: list[str] = []
        memory_ids: tuple[str, ...] = ()
        question_block: QuestionBlock | None = None
        proposal_block: ProposalBlock | None = None
        failed = False
        async for event in self.stream_agent(
            user_id, question, session_id, store=store
        ):
            if isinstance(event, Delta):
                parts.append(event.text)
            elif isinstance(event, QuestionEvent):
                question_block = QuestionBlock(
                    question=event.question, choices=event.choices
                )
            elif isinstance(event, ProposalEvent):
                proposal_block = ProposalBlock(
                    preview=event.preview,
                    accept_token=event.accept_token,
                    action=event.action,
                )
            elif isinstance(event, StreamEnd):
                memory_ids = event.memory_ids
                failed = event.error is not None
        text = "".join(parts).strip()
        if failed and not text:
            return RecallAnswer(failed=True)
        return RecallAnswer(
            text=text,
            memory_ids=memory_ids,
            question=question_block,
            proposal=proposal_block,
        )

    async def stream_agent(
        self, user_id: uuid.UUID, question: str, session_id: str, *, store: bool = True
    ) -> AsyncIterator[StreamEvent]:
        """One agent turn, delivered as it is written.

        `store=False` reads the conversation and writes nothing back. It exists for the
        shadow run, which has to see the same history the real turn saw -- an agent given
        no context is not the agent being evaluated -- while leaving no trace in it. A
        shadow answer appended to the real history would be replayed into the next real
        prompt, so the experiment would start steering the thing it is measuring.
        """
        started = time.monotonic()
        trace = AgentTrace()
        budget = budgets.from_settings()

        snapshot = await self._snapshot(user_id)
        context = (
            f"{CAPABILITY_CARD}\n\n{render_snapshot(snapshot)}"
            if snapshot is not None
            else CAPABILITY_CARD
        )
        toolbox = MemoryToolbox(
            user_id,
            self.repo,
            top_k=settings.TELEGRAM_RECALL_TOP_K,
            budget=budget,
            store=self.store,
            # The person's own message, verbatim. It is the provenance check for
            # `propose_note` and the reason a scraped caption cannot become a note.
            turn=question,
            connections=self.connections,
        )
        if snapshot is not None:
            # The snapshot rows are in the prompt, so by the definition the surfaced set
            # exists to keep, they have been shown: their ids may be cited and opened.
            toolbox.register_snapshot(snapshot.items)

        past = await history.load(session_id)
        # Built before the first search so its allowlist grows as the toolbox surfaces
        # memories. A citation can only be checked against evidence already retrieved.
        checker = StreamValidator(
            allowed_ids=toolbox.allowed_ids,
            allowed_urls=toolbox.allowed_urls,
            max_chars=settings.RECALL_ANSWER_MAX_CHARS,
        )
        spoken: list[str] = []
        ending: graph.AgentEnd | None = None

        async for event in graph.run_agent(
            question, past, toolbox, context=context, budget=budget
        ):
            if isinstance(event, graph.AgentToolCall):
                yield StatusEvent(stage=_STAGES.get(event.name, "working"))
                continue
            if isinstance(event, graph.AgentDelta):
                checker.allowed_ids = list(toolbox.allowed_ids)
                checker.allowed_urls = list(toolbox.allowed_urls)
                released = checker.feed(event.text)
                if released:
                    spoken.append(released)
                    yield Delta(text=released)
                continue
            ending = event

        tail = checker.finish()
        if tail:
            spoken.append(tail)
            yield Delta(text=tail)

        # Emitted whole, after the prose, because a card is a finished thing and the
        # words are what it is about. Only one of the two can be true in a turn: a
        # proposal ends it, and a question ends it.
        if toolbox.proposal is not None:
            yield ProposalEvent(
                preview=toolbox.proposal.preview,
                accept_token=toolbox.proposal.accept_token,
                action=toolbox.proposal.action,
            )
        elif toolbox.question is not None and toolbox.question.choices:
            yield QuestionEvent(
                question=toolbox.question.question, choices=toolbox.question.choices
            )

        trace.rounds = ending.rounds if ending else 0
        trace.tool_calls = list(ending.calls) if ending else []
        trace.cards_in_context = len(toolbox.surfaced)
        trace.exhausted = bool(ending and ending.exhausted)
        trace.final_tool_used = bool(ending and ending.final is not None)
        if ending and ending.final is not None:
            trace.declined_out_of_scope = ending.final.declined_out_of_scope
            trace.asked_question = ending.final.asked_question

        streamed = "".join(spoken).strip()

        if not streamed:
            # Nothing has been shown, so the question is still answerable by another
            # route. Three shapes end up here and only the first is a failure.
            answer = self._unspoken(toolbox, ending)
            if answer is None:
                trace.degraded_to = "single_shot"
                trace.failed = bool(ending and ending.failed)
                trace.duration_ms = int((time.monotonic() - started) * 1000)
                trace.emit()
                async for fallback in super().stream(user_id, question, session_id):
                    yield fallback
                return
            checked = guard(
                answer, toolbox.surfaced, used_tools=bool(trace.tool_calls)
            )
            trace.ids_removed = checked.ids_removed
            trace.urls_removed = checked.urls_removed
            trace.trimmed = checked.trimmed
            trace.flag = checked.flag
            trace.duration_ms = int((time.monotonic() - started) * 1000)
            trace.emit()
            if checked.rejected:
                yield StreamEnd(error=ErrorKind.provider_failure)
                return
            yield Delta(text=checked.text)
            if store:
                await history.append(session_id, question, checked.text)
            yield StreamEnd(memory_ids=toolbox.allowed_ids, corrected=bool(checked.removed))
            return

        trace.ids_removed = sum(
            1 for entry in checker.removed if entry.startswith("unknown-id:")
        )
        trace.urls_removed = sum(
            1 for entry in checker.removed if entry.startswith("unknown-url:")
        )
        trace.trimmed = "length" in checker.removed
        trace.duration_ms = int((time.monotonic() - started) * 1000)
        trace.emit()

        if checker.rejected:
            yield StreamEnd(error=ErrorKind.provider_failure)
            return
        if store:
            await history.append(session_id, question, streamed)
        yield StreamEnd(
            memory_ids=toolbox.allowed_ids, corrected=bool(checker.removed)
        )

    # --- internals --------------------------------------------------------------------

    async def _snapshot(self, user_id: uuid.UUID) -> Snapshot | None:
        """The newest rows, or `None` when they could not be read.

        A failure here costs context, never the answer: the lanes underneath do their own
        reads and fail honestly if those fail.
        """
        if settings.AGENT_SNAPSHOT_ITEMS <= 0:
            return None
        try:
            return await load_snapshot(self.repo, user_id, settings.AGENT_SNAPSHOT_ITEMS)
        except Exception as exc:  # noqa: BLE001 - context is an aid, not the answer
            log.warning("agent_snapshot_failed", error=type(exc).__name__)
            return None

    @staticmethod
    def _unspoken(toolbox: MemoryToolbox, ending: graph.AgentEnd | None) -> str | None:
        """The answer for a turn that streamed nothing, or `None` to fall back.

        A turn can legitimately produce no prose in two ways. It can end by calling
        `AskUser`, in which case the question *is* the reply. Or it can end with
        `FinalAnswer`, whose text travels as tool arguments rather than as tokens and so
        was never streamable. Anything else that said nothing is a failure.
        """
        if toolbox.question is not None:
            return toolbox.question.question
        if ending is not None and ending.final is not None:
            return ending.final.text
        return None


def build_recall_agent(repo: VaultRepository) -> RecallAgentService | None:
    """The agent lane, when this deployment has a chat model and has it switched on."""
    from app.ai.chat.factory import chat_available

    if not settings.AGENT_ENABLED or not chat_available():
        return None
    return RecallAgentService(repo, RedisProposalStore(), _connections(repo))


def _connections(repo: VaultRepository) -> ConnectionReader | None:
    """A connection reader sharing the vault repository's session.

    Built here rather than injected because every caller already hands over a
    `VaultRepository`, and the two have to read inside one transaction -- a second session
    would let the neighbourhood disagree with the snapshot in the same turn.

    `None` when there is no session to share, which is every fake in the suite. That is
    the same degradation the tool already has a name for: no reader, no `GetConnections`,
    and a lane that answers exactly as it did before the tool existed.
    """
    from app.repositories.connection import ConnectionRepository

    session = getattr(repo, "session", None)
    if session is None:
        return None
    return ConnectionRepository(session)
