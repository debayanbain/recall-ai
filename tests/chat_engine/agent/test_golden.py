"""The eight walkthroughs from the design doc, replayed against a recorded model.

Each case is a YAML file: a message, the vault it arrives at, the turns the model takes,
and what the reply must and must not contain. The point is regression cover for the parts
of a turn that no unit test sees together -- the snapshot reaching the prompt, the tools
running in order, the guard checking the result against what was actually surfaced, and
the reply coming back in the language it was asked in.

They are **recorded**, not generated: `_no_provider_calls` still holds, the suite stays
offline, and a case that starts taking network time means a recording is missing. What a
recorded case *cannot* tell you is whether a real model would choose those turns -- that
is what shadow mode is for. What it can tell you is that the harness around it did not
change underneath.

Adding a case is one file. `expect.tool_calls` is the plan, and the rest is the reply.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import settings
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.cards import short_id
from app.services.chat_engine.evidence import RetrievedMemory
from app.services.chat_engine.retrieval import MemoryRetriever
from app.services.chat_engine.trace import AgentTrace
from app.services.chat_engine.types import Delta, ProposalEvent, StreamEnd
from app.services.recall_agent import RecallAgentService
from tests.ai.fakes import RecordedModel, Turn

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_CASES = sorted((Path(__file__).parent / "golden").glob("*.yaml"))


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} is not a mapping"
    return data


def _item(spec: dict[str, Any]) -> VaultItem:
    return VaultItem(
        id=uuid.uuid4(),
        user_id=_USER,
        type=ContentType(spec.get("type", "article")),
        title=spec.get("title"),
        summary=spec.get("summary"),
        content=spec.get("content"),
        source_url=spec.get("url"),
        processing_status=ProcessingStatus(spec.get("status", "completed")),
        created_at=datetime.now(UTC) - timedelta(minutes=int(spec.get("age_minutes", 5))),
    )


class _Repo:
    """The vault this case runs against. Snapshot rows and searchable rows are separate:
    a memory the search finds is not necessarily one of the three newest."""

    def __init__(self, snapshot: Sequence[VaultItem], memories: Sequence[VaultItem]):
        self.snapshot = list(snapshot)
        self.memories = list(memories)

    async def list_for_user(
        self, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> tuple[Sequence[VaultItem], int]:
        return self.snapshot[:limit], len(self.snapshot)

    async def list_filtered(self, user_id: uuid.UUID, **kwargs: Any) -> Any:
        rows = self.snapshot or self.memories
        return rows, len(rows)

    async def get(self, item_id: uuid.UUID, user_id: uuid.UUID) -> VaultItem | None:
        for item in [*self.snapshot, *self.memories]:
            if item.id == item_id and item.user_id == user_id:
                return item
        return None


def _turns(spec: list[dict[str, Any]], ids: dict[str, str]) -> list[Turn]:
    """The recorded turns, with `{{...}}` placeholders resolved to real short ids.

    Ids are generated per run, so a case cannot hard-code one. The placeholders are what
    let a case say "open the memory the search just returned" without the file knowing
    what that id will be.
    """
    turns = []
    for entry in spec:
        calls = [
            (
                call["tool"],
                {
                    key: ids.get(str(value).strip("{} "), value)
                    if isinstance(value, str) and value.startswith("{{")
                    else value
                    for key, value in (call.get("args") or {}).items()
                },
            )
            for call in entry.get("calls") or []
        ]
        turns.append(Turn(text=entry.get("text", ""), calls=calls))
    return turns


class _Store:
    """A proposal store that keeps one row in memory. No Redis in a golden case."""

    def __init__(self) -> None:
        self.minted: list[Any] = []

    async def mint(self, proposal: Any) -> str:
        self.minted.append(proposal)
        return f"tok{len(self.minted)}"

    async def spend(self, token: str, user_id: uuid.UUID) -> None:
        return None


@pytest.mark.parametrize("path", _CASES, ids=lambda p: p.stem)
async def test_golden_case(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _load(path)
    snapshot = [_item(spec) for spec in case.get("snapshot") or []]
    memories = [_item(spec) for spec in case.get("memories") or []]
    ids = {
        "first_id": short_id(memories[0]) if memories else "",
        "first_snapshot_id": short_id(snapshot[0]) if snapshot else "",
    }
    recorded = RecordedModel(_turns(case.get("model_turns") or [], ids))

    import langgraph.prebuilt

    from app.ai.chat import harness

    # What the model was actually shown. A recorded case replays a scripted reply, so
    # asserting on the reply alone cannot tell "the model had the data" from "the script
    # happened to contain it" -- removing the URLs from the snapshot left every case
    # green. This is the half that catches that.
    shown: list[str] = []
    real_run = harness.graph.run_agent

    def _capture(question, history, executor, *, context="", budget):  # type: ignore[no-untyped-def]
        shown.append(context)
        return real_run(question, history, executor, context=context, budget=budget)

    monkeypatch.setattr(harness.graph, "run_agent", _capture)
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", recorded)
    monkeypatch.setattr(harness.graph, "get_agent_model", lambda: object())
    monkeypatch.setattr(settings, "AGENT_ENABLED", True)

    async def _recall(
        self: MemoryRetriever, user_id: uuid.UUID, question: str, filters: Any = None, **kw: Any
    ) -> list[RetrievedMemory]:
        return [RetrievedMemory(item, 0.9) for item in memories]

    async def _load_history(session_id: str) -> list[Any]:
        return list(case.get("history") or [])

    async def _append(session_id: str, question: str, answer: str) -> None:
        return None

    monkeypatch.setattr(MemoryRetriever, "recall", _recall)
    monkeypatch.setattr("app.ai.chat.history.load", _load_history)
    monkeypatch.setattr("app.ai.chat.history.append", _append)

    # The trace is where a turn's self-reported flags end up, so the case reads them from
    # there rather than from a second copy threaded through the return value.
    traces: list[AgentTrace] = []
    original_emit = AgentTrace.emit

    def _capture(self: AgentTrace) -> None:
        traces.append(self)
        original_emit(self)

    monkeypatch.setattr(AgentTrace, "emit", _capture)

    # A case opts into the propose tools; without a store they are not bound at all,
    # which is itself one of the cases.
    store = _Store() if case.get("propose") else None
    service = RecallAgentService(_Repo(snapshot, memories), store)  # type: ignore[arg-type]
    parts: list[str] = []
    ended: StreamEnd | None = None
    offered: ProposalEvent | None = None
    async for event in service.stream_agent(_USER, case["user_text"], "555"):
        if isinstance(event, Delta):
            parts.append(event.text)
        elif isinstance(event, ProposalEvent):
            offered = event
        elif isinstance(event, StreamEnd):
            ended = event
    reply = "".join(parts)

    expect = case.get("expect") or {}
    assert recorded.called == (expect.get("tool_calls") or []), (
        f"{path.name}: the plan changed"
    )
    for needle in expect.get("reply_contains") or []:
        assert needle in reply, f"{path.name}: {needle!r} missing from {reply!r}"
    for needle in expect.get("reply_not_contains") or []:
        assert needle not in reply, f"{path.name}: {needle!r} leaked into the reply"
    if "max_chars" in expect:
        assert len(reply) <= int(expect["max_chars"])
    for needle in expect.get("context_contains") or []:
        assert shown, f"{path.name}: the agent lane never ran"
        assert needle in shown[0], (
            f"{path.name}: {needle!r} was never put in front of the model"
        )
    if "proposal_preview" in expect:
        assert offered is not None, f"{path.name}: nothing was offered"
        assert offered.preview == expect["proposal_preview"]
    if expect.get("no_proposal"):
        assert offered is None, f"{path.name}: something was offered that should not be"
    if "declined" in expect:
        assert traces, f"{path.name}: no turn was traced"
        assert traces[-1].declined_out_of_scope is bool(expect["declined"])
    if expect.get("failed") is False:
        assert ended is not None and ended.error is None
    assert reply.strip(), f"{path.name}: a turn must end with words"


def test_every_case_declares_why_it_exists() -> None:
    """A golden file with no reason in it is one nobody can safely delete or change."""
    assert _CASES, "the golden directory is empty"
    for path in _CASES:
        case = _load(path)
        assert case.get("why", "").strip(), f"{path.name} has no `why`"
        assert case.get("user_text"), f"{path.name} has no `user_text`"
