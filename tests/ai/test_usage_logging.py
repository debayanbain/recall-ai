"""Per-call token accounting, on the existing log stream.

Without this the claim that the refactor shrank prompts is unfalsifiable -- the `len//4`
estimate used to *build* a prompt is the thing being checked, not evidence about it.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.ai.chat.usage import UsageLogger


def _openai_result(prompt: int = 1200, completion: int = 40) -> LLMResult:
    return LLMResult(
        generations=[[ChatGeneration(message=AIMessage(content="hi"))]],
        llm_output={
            "model_name": "gpt-x",
            "token_usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        },
    )


def _gemini_result(input_tokens: int = 900, output_tokens: int = 30) -> LLMResult:
    message = AIMessage(
        content="hi",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        response_metadata={"model_name": "gemini-x"},
    )
    return LLMResult(generations=[[ChatGeneration(message=message)]])


@pytest.fixture
def events() -> Iterator[list[dict[str, Any]]]:
    """Intercept the log stream, then put it back.

    structlog's configuration is process-global, so a test that swaps the processor chain
    and walks away silently rewires logging for every test that runs after it. The
    previous configuration is captured and restored rather than reset to defaults, which
    would drop the JSONL sink the application installs at import.
    """
    captured: list[dict[str, Any]] = []
    previous = structlog.get_config()

    def _processor(logger: Any, name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        captured.append(dict(event_dict))
        raise structlog.DropEvent

    structlog.configure(processors=[structlog.contextvars.merge_contextvars, _processor])
    try:
        yield captured
    finally:
        structlog.configure(**previous)


def test_openai_token_counts_are_recorded(events: list[dict[str, Any]]) -> None:
    handler = UsageLogger("answer")
    handler.on_chat_model_start({}, [])
    handler.on_llm_end(_openai_result())

    assert len(events) == 1
    event = events[0]
    assert event["event"] == "model_call"
    assert event["purpose"] == "answer"
    assert event["model"] == "gpt-x"
    assert event["input_tokens"] == 1200 and event["output_tokens"] == 40
    assert isinstance(event["latency_ms"], int)


def test_gemini_reports_its_counts_somewhere_else_and_is_still_read(
    events: list[dict[str, Any]],
) -> None:
    handler = UsageLogger("converse")
    handler.on_chat_model_start({}, [])
    handler.on_llm_end(_gemini_result())

    assert events[0]["input_tokens"] == 900 and events[0]["output_tokens"] == 30
    assert events[0]["model"] == "gemini-x"


def test_a_provider_that_reports_nothing_logs_none_rather_than_zero(
    events: list[dict[str, Any]],
) -> None:
    """A zero would read as a free call and quietly flatter every measurement."""
    handler = UsageLogger("answer")
    handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x"))]]))

    assert events[0]["input_tokens"] is None and events[0]["output_tokens"] is None


def test_the_surface_and_intent_come_from_context(events: list[dict[str, Any]]) -> None:
    """Bound once by the engine rather than threaded through four call signatures."""
    structlog.contextvars.bind_contextvars(surface="telegram", intent="recall")
    try:
        handler = UsageLogger("answer")
        handler.on_llm_end(_openai_result())
    finally:
        structlog.contextvars.unbind_contextvars("surface", "intent")

    assert events[0]["surface"] == "telegram" and events[0]["intent"] == "recall"


def test_telemetry_never_breaks_the_reply(events: list[dict[str, Any]]) -> None:
    """A logging bug must not turn a working answer into an error."""
    handler = UsageLogger("answer")
    handler.on_llm_end("not an LLMResult")  # type: ignore[arg-type]


def test_a_failed_call_is_recorded_too(events: list[dict[str, Any]]) -> None:
    handler = UsageLogger("answer")
    handler.on_chat_model_start({}, [])
    handler.on_llm_error(RuntimeError("boom"))

    assert events[0]["event"] == "model_call_failed"
    assert events[0]["error"] == "RuntimeError"
