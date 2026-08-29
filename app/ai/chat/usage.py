"""What each model call actually cost, on the project's existing log stream.

The point of the card/budget/history work is that it makes prompts smaller. Without a
number per call that claim is unfalsifiable, and the estimates used to *build* a prompt
(`len // 4`) are not evidence -- they are the thing being checked. This records what the
provider itself reported.

A callback handler rather than a change to the chains, because every chain here ends in
`StrOutputParser()`: by the time a caller has the reply the usage is gone. LangChain hands
the handler the raw `LLMResult`, which still has it.

`surface` and `intent` are not arguments. They arrive through structlog's contextvars,
bound once by the engine, for the same reason `request_id` does -- threading them down
through four call signatures to reach one log line is how those signatures rot.
"""
from __future__ import annotations

import time
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from app.core.logging import get_logger

log = get_logger("ai.usage")


class UsageLogger(BaseCallbackHandler):
    """One `model_call` event per LLM invocation. Never raises into the caller."""

    def __init__(self, purpose: str) -> None:
        self.purpose = purpose
        self._started_at: float | None = None

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any
    ) -> None:
        self._started_at = time.perf_counter()

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: Any, **kwargs: Any
    ) -> None:
        self._started_at = time.perf_counter()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        try:
            self._emit(response)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a reply
            log.debug("usage_log_failed", error=type(exc).__name__)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        log.info(
            "model_call_failed",
            purpose=self.purpose,
            error=type(error).__name__,
            latency_ms=self._latency_ms(),
        )

    def _latency_ms(self) -> int | None:
        if self._started_at is None:
            return None
        return int((time.perf_counter() - self._started_at) * 1000)

    def _emit(self, response: LLMResult) -> None:
        tokens = _token_counts(response)
        log.info(
            "model_call",
            purpose=self.purpose,
            model=_model_name(response),
            input_tokens=tokens.get("input"),
            output_tokens=tokens.get("output"),
            latency_ms=self._latency_ms(),
        )


def _model_name(response: LLMResult) -> str | None:
    """Providers disagree about where the model name goes; try each in turn."""
    output = response.llm_output or {}
    for key in ("model_name", "model"):
        value = output.get(key)
        if isinstance(value, str):
            return value
    for generations in response.generations:
        for generation in generations:
            meta = getattr(getattr(generation, "message", None), "response_metadata", None)
            if isinstance(meta, dict):
                for key in ("model_name", "model"):
                    if isinstance(meta.get(key), str):
                        return str(meta[key])
    return None


def _token_counts(response: LLMResult) -> dict[str, int | None]:
    """Read the provider's own counts, from whichever shape it used.

    OpenAI reports `token_usage` on `llm_output`; Gemini attaches `usage_metadata` to the
    message. Both are read rather than one being normalised at the factory, because a
    missing count must stay missing -- a zero here would read as a free call.
    """
    output = response.llm_output or {}
    usage = output.get("token_usage") or output.get("usage") or {}
    if isinstance(usage, dict) and usage:
        return {
            "input": _first_int(usage, "prompt_tokens", "input_tokens"),
            "output": _first_int(usage, "completion_tokens", "output_tokens"),
        }

    for generations in response.generations:
        for generation in generations:
            meta = getattr(getattr(generation, "message", None), "usage_metadata", None)
            if isinstance(meta, dict) and meta:
                return {
                    "input": _first_int(meta, "input_tokens", "prompt_tokens"),
                    "output": _first_int(meta, "output_tokens", "completion_tokens"),
                }
    return {"input": None, "output": None}


def _first_int(source: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, int):
            return value
    return None
