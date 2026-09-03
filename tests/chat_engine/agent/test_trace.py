"""One line per turn, and the two things it must not do.

A trace is only worth having if it can be read at the moment someone needs it, so what is
pinned here is the shape -- one record, tool names only -- and the rule that telemetry can
never cost anyone their reply.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.ai.prompts import PROMPT_VERSION
from app.services.chat_engine import trace as trace_module
from app.services.chat_engine.trace import AgentTrace


class _Recorder:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.lines.append((event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self.lines.append((event, fields))


@pytest.fixture
def _log(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(trace_module, "log", recorder)
    return recorder


def test_a_turn_is_one_record(_log: _Recorder) -> None:
    """Not one line per step: a turn is the unit a person experienced."""
    AgentTrace(rounds=2, tool_calls=["SearchMemories", "GetMemory"]).emit()

    assert len(_log.lines) == 1
    event, fields = _log.lines[0]
    assert event == "agent_turn"
    assert fields["rounds"] == 2
    assert fields["tools_used"] == 2


def test_the_prompt_version_rides_on_every_turn(_log: _Recorder) -> None:
    """Without it a regression cannot be dated, which is most of what a trace is for."""
    AgentTrace().emit()

    assert _log.lines[0][1]["prompt_version"] == PROMPT_VERSION


def test_only_tool_names_are_recorded(_log: _Recorder) -> None:
    """The arguments are derived from the person's message; a log carrying them carries it."""
    AgentTrace(tool_calls=["SearchMemories"]).emit()

    rendered = repr(_log.lines[0][1])
    assert "SearchMemories" in rendered
    assert "query" not in rendered


def test_the_self_reports_are_recorded_and_not_acted_on(_log: _Recorder) -> None:
    """Both booleans come from the model's own FinalAnswer. They are evidence, not control."""
    AgentTrace(declined_out_of_scope=True, asked_question=True).emit()

    fields = _log.lines[0][1]
    assert fields["declined_out_of_scope"] is True
    assert fields["asked_question"] is True


def test_the_guard_result_travels_as_one_object(_log: _Recorder) -> None:
    AgentTrace(ids_removed=1, urls_removed=2, trimmed=True, flag="x").emit()

    assert _log.lines[0][1]["guard"] == {
        "ids_removed": 1,
        "urls_removed": 2,
        "trimmed": True,
        "flag": "x",
    }


def test_a_broken_log_call_never_costs_a_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn is already answered by the time this runs. It must not be able to undo it."""

    class _Broken:
        def info(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("the log sink is gone")

        def warning(self, *args: Any, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(trace_module, "log", _Broken())

    AgentTrace().emit()  # must not raise
