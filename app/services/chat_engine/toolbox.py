"""What the memory tools actually do. One user, one turn, every bound in one place.

`app/ai/chat/tools.py` declares what the model may ask for; this is what happens when it
asks. The split is the same one the rest of this codebase makes between a prompt and the
thing it is a prompt for -- and here it carries the security boundary, because a tool is
the one place a model's output becomes an action.

Four properties, and none of them is expressible as a tool argument, which is the point:

* **The user is fixed at construction.** `user_id` comes from the caller that resolved
  the account -- never from the model, never from the message, never from a memory. A
  prompt injection cannot ask for another tenant's rows because there is no argument to
  ask with, and the repository re-applies the predicate underneath anyway.
* **Ids are capabilities, and only surfaced ones exist.** `get_memory` reads `_seen`,
  which holds exactly the memories a search or a list has already returned *in this
  turn*. A short id is a prefix of a UUID the owner already has and reaches nothing on
  its own; restricting it further is what stops the model spending calls on ids a memory
  told it to open.
* **The relevance gate is not the model's to skip.** Every search goes through
  `evidence.assess` before its results are rendered, exactly as the single-shot path
  does. The model chose the query; it does not get to choose what counts as a match. A
  weak set is handed back labelled weak rather than silently upgraded.
* **Arguments are validated here, next to the query.** `content_types` is checked against
  the real enum and `days` is bounded, because these values reach a SQL filter and a
  hallucinated one would otherwise arrive there intact. Validating in the schema instead
  would put the check somewhere a second caller can skip.

Everything returned is *text the model will read*, fenced with `chain.fence_block` -- the
same fence the single-shot path uses. A tool result is quoted material for the same
reason a retrieved memory is: it came from the same scraped pages.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect as sa_inspect

from app.ai.chat import chain
from app.core.logging import get_logger
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.repositories.vault import VaultRepository
from app.services.chat_engine import status
from app.services.chat_engine.budget import MORE_FMT, SPENT, Budget
from app.services.chat_engine.cards import (
    DETAIL_CONTENT_LIMIT,
    DETAIL_MAX_ITEMS,
    MAX_TAGS,
    SUMMARY_LIMIT,
    build_card,
    build_detail_card,
    memory_link,
    short_id,
)
from app.services.chat_engine.evidence import EvidenceStatus, assess
from app.services.chat_engine.proposals import (
    Action,
    Proposal,
    ProposalStore,
    from_user_turn,
)
from app.services.chat_engine.retrieval import (
    DEFAULT_LIMIT,
    MemoryFilters,
    MemoryRetriever,
)
from app.services.chat_engine.types import Choice, ProposalBlock, QuestionBlock

log = get_logger("recall.chat")

#: The same ceiling the planner applies. A model that answers "how many days is 'a while
#: ago'" with 40000 is not describing a period anyone has been saving things for.
_MAX_DAYS = 3650

#: Query text is echoed into a log line and into a fixed reply. Bounded for the same
#: reason every other model output here is.
_MAX_QUERY = 500

#: How many memories a listing returns. Smaller than a search's top-k on purpose: a
#: listing has no relevance ordering to trust, so a long one is a long way to say
#: "here is everything".
_LIST_LIMIT = 10

#: What a tool says when it found nothing. Plain English, addressed to the model rather
#: than to the user -- the model translates its own reply, and the fixed sentence the
#: *user* sees when nothing at all was found is written in `recall_chat`.
_NO_MATCH = (
    "No memories matched. If you have not already tried different words, search once "
    "more; otherwise tell them you could not find it."
)


#: A question is a reply, so it is bounded like one. Long enough for "did you mean the
#: Docker talk or the Kubernetes one?", short enough that it cannot become an essay.
_MAX_QUESTION = 300

#: Ceilings for the options offered alongside a question. Kept next to the schema's own
#: limits rather than duplicated: the schema asks the model for at most this many, and
#: this is what happens when it asks for more anyway.
ASK_USER_MAX_OPTIONS = 4
ASK_USER_MAX_OPTION_CHARS = 40

#: What `ask_user` hands back. The graph reads it as "this turn ends with a question"
#: rather than as evidence -- it is a control signal that happens to travel as a tool
#: result, because a tool that returned nothing would leave the call unanswered.
ASKED = (
    "Your question has been put to the person and the turn is over. Do not call any "
    "more tools and do not write anything further."
)


#: A note offered by tap is bounded like a typed one. The capture path clips again.
_MAX_NOTE = 4000

#: Returned when nothing can park a proposal -- no store wired up, or the store could not
#: be reached. The model is told plainly rather than left to infer it from a failure, and
#: the prompt already covers what to say when it cannot offer to write.
_NO_STORE = (
    "You cannot offer to save or retry anything right now. Tell the person how to do it "
    "themselves: /note <text> keeps a thought, and a failed capture can be retried from "
    "the web app."
)

#: The only two states a retry can improve. Re-running a completed item spends the whole
#: pipeline to replace a result with itself; re-running a queued one races the worker.
_RETRYABLE = (ProcessingStatus.failed, ProcessingStatus.skipped)


class SurfacedSet:
    """Every memory this turn has actually put in front of the model.

    It is the answer to two different questions and it must stay the same answer to both:
    which ids the reply may cite, and which ids `GetMemory` may open. Both are the same
    claim -- "the model saw this" -- so they read one structure. Anything shown to the
    model without landing here would have its own citation stripped on the way out as a
    fabrication, which is a confusing bug to chase; anything landing here without being
    shown is a capability handed out for free.

    Seeded by the snapshot and added to by tool results. Nothing else may write to it.
    """

    __slots__ = ("_items",)

    def __init__(self) -> None:
        #: short id -> the row it names. Insertion-ordered, so `ids` follows the order
        #: the model was shown them in.
        self._items: dict[str, VaultItem] = {}

    def add(self, item: VaultItem) -> str:
        """Record one row as seen and return the id it is known by.

        `setdefault`, not assignment: the first sighting wins, so a row that comes back
        from a second search keeps the object the model was originally shown.
        """
        identifier = short_id(item)
        self._items.setdefault(identifier, item)
        return identifier

    def get(self, identifier: str) -> VaultItem | None:
        return self._items.get(identifier.strip().lower())

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._items)

    @property
    def urls(self) -> tuple[str | None, ...]:
        """Every URL an answer about these memories may legitimately contain.

        **Both links per memory.** `validate_answer` replaces any URL outside this set
        with `[link omitted]`, so a vault page missing from here is a correct answer the
        guard silently deletes -- the model does everything right and the person is handed
        a reply with a hole in it. That failure is invisible from the model's side and
        looks like a model problem, which is why the two are built from one place.
        """
        urls: list[str | None] = []
        for item in self._items.values():
            urls.append(item.source_url)
            urls.append(memory_link(item))
        return tuple(urls)

    @property
    def items(self) -> list[VaultItem]:
        return list(self._items.values())

    def __len__(self) -> int:
        return len(self._items)


class MemoryToolbox:
    """The tools for one user, for one question. Not reusable across turns.

    Deliberately short-lived: `_seen` is the set of memories this answer is allowed to
    cite, and carrying it into the next question would let an answer cite evidence that
    was retrieved for a different one.
    """

    def __init__(
        self,
        user_id: uuid.UUID,
        repo: VaultRepository,
        *,
        top_k: int | None = None,
        budget: Budget | None = None,
        store: ProposalStore | None = None,
        turn: str = "",
    ) -> None:
        self.user_id = user_id
        self.repo = repo
        self.memories = MemoryRetriever(repo)
        self.top_k = top_k or DEFAULT_LIMIT
        #: What this turn has shown the model, and therefore what it may cite and open.
        self.surfaced = SurfacedSet()
        #: The turn's allowance. `None` is unbounded, which is what the older tool lane
        #: has always been -- it is bounded by its own loop instead, and giving it a
        #: budget here would apply two ceilings to one turn.
        self.budget = budget
        #: Set by `ask_user`, read by the caller. `None` means the turn ended with an
        #: answer rather than with a question.
        self.question: QuestionBlock | None = None
        #: Set by a `propose_*` tool. The caller renders it as a confirmation card; the
        #: write itself happens somewhere with no model in it.
        self.proposal: ProposalBlock | None = None
        #: Where a proposed action is parked until somebody taps it. `None` means this
        #: turn cannot offer one, and the tools are simply not bound.
        self.store = store
        #: **The person's own message, verbatim.** It is the provenance check for
        #: `propose_note` and nothing else reads it. A model reading a scraped caption
        #: can be talked into proposing anything; it cannot be talked into having been
        #: asked, because a memory cannot edit what the person typed.
        self.turn = turn
        #: What the model actually searched for, in order. The first entry is the best
        #: available description of the subject when nothing was found at all -- it is
        #: the model's own extraction of it, which is what the planner used to produce.
        self.queries: list[str] = []
        #: How many times the model actually went looking -- a search or a listing. It is
        #: not `len(queries)`: a listing has no query text and still counts as having
        #: looked. What it separates is "searched and found nothing" from "never
        #: searched", which are two different answers and used to be one.
        self.lookups = 0

    # --- what the answer is allowed to have said --------------------------------------

    @property
    def allowed_ids(self) -> tuple[str, ...]:
        return self.surfaced.ids

    @property
    def allowed_urls(self) -> tuple[str | None, ...]:
        return self.surfaced.urls

    @property
    def items(self) -> list[VaultItem]:
        return self.surfaced.items

    @property
    def found_nothing(self) -> bool:
        """True when the model went looking and came back with nothing.

        Both halves matter. An empty surfaced set alone was wrong the moment the prompt
        gained a vault snapshot: a question the snapshot answers ("what were my last
        three?", "what can you do?") is answered with **no tool call at all**, and by this
        property's old reading that was indistinguishable from a failed search -- so a
        correct answer was thrown away and replaced with "I could not find anything about
        that in your vault", about memories the person was looking straight at.
        """
        return self.lookups > 0 and not self.surfaced

    # --- the tools ---------------------------------------------------------------------

    async def search_memories(
        self,
        query: str,
        days: int | None = None,
        content_types: Sequence[str] = (),
        category: str | None = None,
    ) -> str:
        if (spent := self._spend()) is not None:
            return spent
        text = (query or "").strip()[:_MAX_QUERY]
        if not text:
            # An empty search is the model reaching for a listing. Answered as one
            # rather than refused: a refusal costs a round to say what a redirect says.
            return await self.list_memories(days, content_types, category)

        self.queries.append(text)
        self.lookups += 1
        memories = await self.memories.recall(
            self.user_id,
            text,
            MemoryFilters(
                created_after=_created_after(days),
                content_types=_content_types(content_types),
                category=_category(category),
            ),
            limit=self.top_k,
        )
        evidence = assess(memories)
        log.info(
            "recall_tool_search",
            retrieved=len(memories),
            kept=len(evidence.memories),
            best=round(evidence.best_score, 3),
            status=evidence.status.value,
        )
        if evidence.status is EvidenceStatus.no_evidence:
            return _NO_MATCH

        blocks = self._render(evidence.items)
        if evidence.status is EvidenceStatus.insufficient:
            # The same distinction `GUIDANCE_WEAK` draws on the single-shot path, said
            # per result rather than once for the turn: with several searches in a turn,
            # one system-level caveat cannot say which of them it is about.
            return (
                "These are only a WEAK match. Do not stretch them to fit -- say you "
                "found related memories but nothing that answers it precisely.\n\n"
                f"{blocks}"
            )
        return blocks

    async def list_memories(
        self,
        days: int | None = None,
        content_types: Sequence[str] = (),
        category: str | None = None,
        status: str | None = None,
    ) -> str:
        """Newest-first, no embedding and no vector scan.

        The cheap half of retrieval, and the right answer to "what did I save this
        week?": there is no subject to rank against, so paying for an embedding buys an
        ordering that means nothing.
        """
        if (spent := self._spend()) is not None:
            return spent
        self.lookups += 1
        items, total = await self.repo.list_filtered(
            self.user_id,
            limit=_LIST_LIMIT,
            created_after=_created_after(days),
            content_types=_content_types(content_types),
            category=_category(category),
            statuses=_statuses(status),
        )
        log.info("recall_tool_list", returned=len(items), total=total)
        if not items:
            return _NO_MATCH
        header = f"{total} saved in total; the {len(items)} newest are below.\n\n"
        return header + self._render(items)

    async def get_memory(self, memory_id: str) -> str:
        """One memory's own text -- only for an id already surfaced in this turn.

        The restriction is not about secrecy: a short id is a prefix of a UUID whose row
        this user owns, and the repository would scope it regardless. It is about a model
        that has been told by a *memory* to open something. An id it has not been handed
        is one it did not get from the vault.
        """
        if (spent := self._spend()) is not None:
            return spent
        wanted = (memory_id or "").strip().lower()
        item = self.surfaced.get(wanted)
        if item is None:
            log.info("recall_tool_unknown_id", requested=wanted[:16])
            return (
                "No memory with that id has been returned to you. Use only ids from "
                "the blocks above, or search first."
            )
        item = await self._with_body(item)
        return chain.fence_block(
            short_id(item),
            build_detail_card(item),
            title=item.title or item.source_url or "Untitled",
            category=item.ai_category,
            saved=item.created_at.date().isoformat() if item.created_at else None,
            url=item.source_url,
            link=memory_link(item),
        )

    async def query_memories(
        self,
        text: str | None = None,
        days: int | None = None,
        content_types: Sequence[str] = (),
        category: str | None = None,
        status: str | None = None,
        tags: Sequence[str] = (),
        limit: int | None = None,
        fields: Sequence[str] = (),
    ) -> str:
        """One read the model composes itself: which rows, and which fields come back.

        It replaces the fixed `SearchMemories` / `ListMemories` pair on the agent lane.
        The pair was not short of *power* -- it was short of expressiveness, and the model
        had no way to say "those two, with their links". Composing the query is the honest
        version of "the agent makes its own tool": it chooses the filters and the
        projection, and nothing anywhere executes text the model wrote.

        Two things are deliberately **not** the model's to choose:

        * **The links are always returned**, in the header, never behind a `fields` opt-in.
          A projection that could omit them is a projection that can reproduce the bug this
          exists to fix -- an answer saying it cannot provide a link for a memory that has
          two.
        * **The tenant.** `user_id` is bound on this object, as it is for every other tool.

        `text` chooses the path, mirroring the split the two old tools made: with it, a
        ranked semantic search through the same relevance gate; without it, a plain
        newest-first listing, which is the right answer to a purely time- or kind-scoped
        question and costs no embedding.
        """
        if (spent := self._spend()) is not None:
            return spent

        wanted = _fields(fields)
        subject = (text or "").strip()[:_MAX_QUERY]
        rows = _limit(limit)
        self.lookups += 1

        if subject:
            self.queries.append(subject)
            memories = await self.memories.recall(
                self.user_id,
                subject,
                MemoryFilters(
                    created_after=_created_after(days),
                    content_types=_content_types(content_types),
                    category=_category(category),
                ),
                limit=max(rows, self.top_k),
            )
            evidence = assess(memories)
            log.info(
                "recall_tool_query",
                mode="search",
                retrieved=len(memories),
                kept=len(evidence.memories),
                best=round(evidence.best_score, 3),
                status=evidence.status.value,
                fields=list(wanted),
            )
            if evidence.status is EvidenceStatus.no_evidence:
                return _NO_MATCH
            items = _with_tags(evidence.items, tags)[:rows]
            if not items:
                return _NO_MATCH
            blocks = self._render(items, wanted)
            if evidence.status is EvidenceStatus.insufficient:
                return (
                    "These are only a WEAK match. Do not stretch them to fit -- say you "
                    "found related memories but nothing that answers it precisely.\n\n"
                    f"{blocks}"
                )
            return blocks

        found, total = await self.repo.list_filtered(
            self.user_id,
            limit=rows,
            created_after=_created_after(days),
            content_types=_content_types(content_types),
            category=_category(category),
            tags=list(tags) or None,
            statuses=_statuses(status),
        )
        log.info(
            "recall_tool_query",
            mode="list",
            returned=len(found),
            total=total,
            fields=list(wanted),
        )
        if not found:
            return _NO_MATCH
        header = f"{total} saved in total; {len(found)} shown below.\n\n"
        return header + self._render(found, wanted)

    async def get_capture_status(self, memory_id: str | None = None) -> str:
        """Whether a capture finished, is still being read, or failed.

        The same reading `status.py` gives the deterministic lane, handed to the model as
        a fact rather than left for it to infer from a card. It costs no model call and
        it cannot be wrong: the row is the authority on its own state.

        `processing_error` is never included. It is a provider's phrasing about our
        infrastructure, it is scrubbed on the way into the database precisely because it
        is not for a person to read, and putting it here would put it in front of one.
        """
        if (spent := self._spend()) is not None:
            return spent

        if memory_id:
            item = self.surfaced.get(memory_id)
            if item is None:
                log.info("recall_tool_unknown_id", requested=memory_id[:16])
                return (
                    "No memory with that id has been shown to you. Use an id from the "
                    "snapshot or from a search, or leave it empty for the newest one."
                )
            return status.describe([item], "")

        items, _total = await self.repo.list_for_user(self.user_id, limit=status.LOOKBACK)
        for item in items:
            self.surfaced.add(item)
        return status.describe(list(items), "")

    async def ask_user(self, question: str, options: Sequence[str] = ()) -> str:
        """Record a question back to the person. Returns the sentinel the graph reads.

        Not charged against the tool budget: asking is how a turn *ends*, so billing it
        to the search allowance would let a model that has run out of searches also run
        out of ways to say "which one did you mean?".

        Each offered option gets its own single-use token. The token, not the text, is
        what a tapped button carries -- a callback payload is 64 bytes and an option may
        be a sentence in any script -- and the text it stands for is handed back through
        the ordinary inbound path, so an answered question is an ordinary turn.
        """
        labels = [
            option.strip()[:ASK_USER_MAX_OPTION_CHARS]
            for option in list(options)[:ASK_USER_MAX_OPTIONS]
            if option and option.strip()
        ]
        choices: list[Choice] = []
        if self.store is not None:
            for label in labels:
                token = await self.store.mint(
                    Proposal(self.user_id, Action.answer, {"text": label})
                )
                if token is not None:
                    choices.append(Choice(label=label, token=token))
        self.question = QuestionBlock(
            question=question.strip()[:_MAX_QUESTION], choices=tuple(choices)
        )
        return ASKED

    async def propose_note(self, text: str) -> str:
        """Offer to save a note. Writes nothing.

        The mint-time provenance check is the security boundary of this whole feature.
        Tool results are scraped captions and page bodies -- the text an attacker gets to
        write -- so a model that has just read one can be argued into proposing anything
        it says. What it cannot be argued into is the person having *asked*: the check is
        that the words appear in their own message this turn. A note whose text lives only
        inside a tool result is refused here and logged as `proposal_text_not_from_user`,
        which is the clearest injection signal this system has.
        """
        wanted = (text or "").strip()[:_MAX_NOTE]
        if not wanted:
            return "There is nothing to save. Ask them what they want kept."
        if self.store is None:
            return _NO_STORE
        if not from_user_turn(wanted, self.turn):
            log.warning("proposal_text_not_from_user")
            return (
                "Refused: those words are not in what the person wrote this turn. You "
                "may only offer to save something they asked for themselves. Tell them "
                "they can send it with /note."
            )
        token = await self.store.mint(
            Proposal(self.user_id, Action.note, {"text": wanted})
        )
        if token is None:
            return _NO_STORE
        self.proposal = ProposalBlock(preview=wanted, accept_token=token, action="note")
        log.info("proposal_minted", action="note")
        return (
            "Offered. The person will see the exact text with a Yes/No button. Tell "
            "them what you are offering to save and stop -- do not claim it is saved."
        )

    async def propose_delete(self, memory_id: str) -> str:
        """Offer to remove a memory. Writes nothing.

        The refusal is the same one `get_memory` and `propose_retry` use, and it is the
        one that matters most here: **only an id this turn actually surfaced**. An id a
        *memory* mentioned did not come from the vault, and delete is the one action
        where acting on an attacker-chosen id destroys something.

        Unlike a note there is no text to check against the person's own turn, so the
        card carries the memory's title instead -- what a person needs to see before
        tapping is *which* memory, and a title is that.
        """
        item = self.surfaced.get(memory_id or "")
        if item is None:
            log.info("recall_tool_unknown_id", requested=(memory_id or "")[:16])
            return (
                "No memory with that id has been shown to you. Use an id from the "
                "snapshot or from a search."
            )
        if self.store is None:
            return _NO_STORE
        token = await self.store.mint(
            Proposal(self.user_id, Action.delete, {"memory_id": str(item.id)})
        )
        if token is None:
            return _NO_STORE
        preview = item.title or item.source_url or short_id(item)
        self.proposal = ProposalBlock(
            preview=preview, accept_token=token, action="delete"
        )
        log.info("proposal_minted", action="delete")
        return (
            "Offered. The person will see which memory, with a Yes/No button. Name it, "
            "say that deleting is permanent, and stop -- do not claim it is deleted."
        )

    async def propose_retry(self, memory_id: str) -> str:
        """Offer to re-run a capture that did not work. Writes nothing.

        Two refusals, both about not spending a whole pipeline to reproduce a result that
        already exists: the id has to be one this turn actually surfaced, and the row has
        to be in a state a retry can improve.
        """
        item = self.surfaced.get(memory_id or "")
        if item is None:
            log.info("recall_tool_unknown_id", requested=(memory_id or "")[:16])
            return (
                "No memory with that id has been shown to you. Use an id from the "
                "snapshot or from a search."
            )
        if item.processing_status not in _RETRYABLE:
            return (
                f"That one is {item.processing_status.value}, so there is nothing to "
                "retry. Only a failed or skipped capture can be re-run."
            )
        if self.store is None:
            return _NO_STORE
        token = await self.store.mint(
            Proposal(self.user_id, Action.retry, {"memory_id": str(item.id)})
        )
        if token is None:
            return _NO_STORE
        preview = item.title or item.source_url or short_id(item)
        self.proposal = ProposalBlock(
            preview=preview, accept_token=token, action="retry"
        )
        log.info("proposal_minted", action="retry")
        return (
            "Offered. The person will see a Yes/No button. Say what you are offering to "
            "retry and stop -- do not claim it has been retried."
        )

    def register_snapshot(self, items: Sequence[VaultItem]) -> None:
        """Seed the surfaced set from the rows already in the prompt.

        The snapshot is shown to the model, so by the definition this set exists to keep,
        those rows *have* been surfaced: their ids may be cited and opened. Without this
        the model would be shown three memories and then have its citation of one of them
        deleted as a fabrication -- correct behaviour applied to the wrong input.
        """
        for item in items:
            self.surfaced.add(item)

    # --- internals ------------------------------------------------------------------------

    def _spend(self) -> str | None:
        """One unit of the tool allowance, or the sentence to return instead of working.

        Returning a string rather than raising is the whole point: the caller hands it
        back as an ordinary tool result, so every call the model made still gets its
        `ToolMessage` and the conversation stays well-formed.
        """
        if self.budget is None:
            return None
        if self.budget.spend_call():
            return None
        log.info(
            "recall_tool_budget_spent",
            calls=self.budget.calls_used,
            elapsed_ms=int(self.budget.elapsed * 1000),
        )
        return SPENT

    async def _with_body(self, item: VaultItem) -> VaultItem:
        """The same row, guaranteed to have its body loaded.

        Snapshot rows arrive from a `cards_only` listing, which is `load_only(...,
        raiseload=True)`: reading `content` off one raises rather than quietly emitting a
        per-row query. That is the right default for a listing and the wrong one here, so
        this is the single place that re-reads -- scoped to the owner, like every other
        read, so a re-read can never widen what `get_memory` could already reach.
        """
        state = sa_inspect(item)
        # A row built in memory (a test's fixture, or anything not fetched from a
        # session) has no unloaded set at all; it is fully populated by construction.
        if state is None or "content" not in state.unloaded:
            return item
        full = await self.repo.get(item.id, self.user_id)
        return full or item

    # --- rendering ----------------------------------------------------------------------

    def _render(
        self, items: Sequence[VaultItem], fields: tuple[str, ...] | None = None
    ) -> str:
        """Cards as fenced blocks, registering each one as citable evidence.

        Registration happens here rather than at the call sites so a tool that returns
        blocks without recording them is not a thing anyone can write: the validator
        checks the answer's citations against `allowed_ids`, and a block shown to the
        model but missing from that set would have its own citation stripped as a
        fabrication.
        """
        allowed = items[: DETAIL_MAX_ITEMS * 4]
        if self.budget is not None:
            keep = self.budget.take_cards(len(allowed))
            dropped = len(allowed) - keep
            allowed = allowed[:keep]
        else:
            dropped = 0

        blocks = []
        for item in allowed:
            identifier = self.surfaced.add(item)
            blocks.append(
                chain.fence_block(
                    identifier,
                    build_card(item) if fields is None else _project(item, fields),
                    title=item.title or item.source_url or "Untitled",
                    category=item.ai_category,
                    saved=item.created_at.date().isoformat() if item.created_at else None,
                    url=item.source_url,
                    link=memory_link(item),
                )
            )
        rendered = "\n\n".join(blocks)
        return rendered + MORE_FMT.format(count=dropped) if dropped else rendered


# --- argument validation ------------------------------------------------------------------
#
# Model output on its way to a SQL filter. Every one of these silently drops what it does
# not recognise rather than raising: a hallucinated content type should cost the model a
# narrower search, not the user their answer.


def _created_after(days: int | None) -> datetime | None:
    if days is None or not (1 <= days <= _MAX_DAYS):
        return None
    return datetime.now(UTC) - timedelta(days=days)


def _content_types(values: Sequence[str]) -> list[ContentType] | None:
    valid = {t.value for t in ContentType}
    resolved = [ContentType(v) for v in values if isinstance(v, str) and v in valid]
    return resolved or None


#: What a caller may ask a query to return, beyond the header every result carries.
#: A closed list because it reaches a renderer and a token budget: `excerpt` is the
#: expensive one, and it being opt-in is what keeps a ten-row listing from shipping ten
#: article bodies. `title`, `url` and `link` are absent on purpose -- they are always in
#: the header, so no projection can produce a result that cannot be handed over.
QUERY_FIELDS = (
    "summary",
    "tags",
    "category",
    "saved",
    "status",
    "age",
    "excerpt",
)

#: What a caller gets for asking for nothing: enough to tell memories apart, cheaply.
_DEFAULT_FIELDS = ("summary", "tags", "status", "age")

#: Rows per query. The card ceiling still applies on top, across the whole turn.
_MAX_QUERY_ROWS = 20


def _fields(values: Sequence[str]) -> tuple[str, ...]:
    """The requested fields, filtered to the ones that exist.

    Unknown names are dropped rather than refused, like `_content_types` above: the value
    is a model's guess at a word, and a result with one field missing is a far better
    answer than an error the model has to interpret and retry.
    """
    wanted = tuple(
        name for name in dict.fromkeys(str(v).strip().lower() for v in values)
        if name in QUERY_FIELDS
    )
    return wanted or _DEFAULT_FIELDS


def _limit(value: int | None) -> int:
    if not value or value < 1:
        return _LIST_LIMIT
    return min(int(value), _MAX_QUERY_ROWS)


def _with_tags(items: Sequence[VaultItem], tags: Sequence[str]) -> list[VaultItem]:
    """Apply a tag filter to ranked results.

    Done here rather than in SQL because `search_semantic` has no tag predicate -- it
    orders by vector distance over the chunk index, and adding a JSONB containment filter
    to that query is what makes the planner drop the HNSW index. The set is at most
    `top_k` rows, so filtering them in Python costs nothing.
    """
    wanted = {tag.strip().lower() for tag in tags if tag and tag.strip()}
    if not wanted:
        return list(items)
    return [
        item
        for item in items
        if wanted <= {str(tag).lower() for tag in (item.ai_tags or [])}
    ]


def _project(item: VaultItem, fields: tuple[str, ...]) -> str:
    """A card body carrying only what was asked for.

    Every value is flattened to one line and length-capped, for the same reason
    `cards.build_card` does it: an item's own text must not be able to open a line that
    looks like the start of another memory.
    """
    lines: list[str] = []
    for name in fields:
        rendered = _field(item, name)
        if rendered:
            lines.append(f"  {name}: {rendered}")
    return "\n".join(lines) or "  (no details requested)"


def _field(item: VaultItem, name: str) -> str | None:
    if name == "summary":
        return _one_line(item.summary, SUMMARY_LIMIT)
    if name == "tags":
        tags = [str(tag) for tag in (item.ai_tags or [])][:MAX_TAGS]
        return ", ".join(tags) or None
    if name == "category":
        return _one_line(item.ai_category, 60)
    if name == "saved":
        return item.created_at.date().isoformat() if item.created_at else None
    if name == "status":
        return str(getattr(item.processing_status, "value", item.processing_status))
    if name == "age":
        return _age_of(item)
    if name == "excerpt":
        return _one_line(item.content, DETAIL_CONTENT_LIMIT)
    return None


def _one_line(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    collapsed = " ".join(str(value).split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return collapsed or None


def _age_of(item: VaultItem) -> str | None:
    """Relative, like the snapshot's -- nothing here knows the reader's timezone."""
    if item.created_at is None:
        return None
    moment = item.created_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - moment).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _statuses(value: str | None) -> list[ProcessingStatus] | None:
    """One processing state, checked against the enum. Anything else means "all of them".

    Unrecognised is dropped rather than refused, like `_content_types` above it: the value
    is a model's guess at a word, and a listing of everything is a worse answer than a
    filtered one but a much better answer than an error the person has to interpret.
    """
    if not value:
        return None
    try:
        return [ProcessingStatus(value.strip().lower())]
    except ValueError:
        log.info("recall_tool_unknown_status", value=value[:32])
        return None


def _category(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned[:64] or None
