"""Failing over to the other configured provider, and the three things it must not do.

Selecting a provider and surviving one are different questions. `AI_PROVIDER` naming
something unbuildable is an error -- answering with a model the operator did not choose
is not a recovery. A 500 from a provider they *did* choose, when they configured a second
one too, is a different case: the reply should still arrive.

What the fallback must never do is provision itself (no key, no fallback), reach
embeddings (two providers' vectors are not comparable), or cost the tool lane and the
planner their model-level features.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import Runnable, RunnableLambda

from app.ai.chat import factory
from app.core.config import settings

#: Captured at import, before `tests/conftest.py`'s autouse no-provider guard replaces
#: it. That guard is what stops a stray code path billing a real key during a test run;
#: the selection tests below are *about* the function it replaces, so they hold the
#: original.
_REAL_FALLBACKS = factory.fallback_models


class _Model(RunnableLambda[Any, str]):
    """Stands in for a chat model: answers with its own name, or raises."""

    def __init__(self, name: str, *, fails: bool = False) -> None:
        def _run(_input: Any) -> str:
            if fails:
                raise RuntimeError(f"{name} is down")
            return name

        super().__init__(_run)


def _providers(
    monkeypatch: pytest.MonkeyPatch, primary: _Model, alternates: tuple[_Model, ...]
) -> None:
    monkeypatch.setattr(factory, "get_chat_model", lambda: primary)
    monkeypatch.setattr(factory, "fallback_models", lambda: alternates)


def _identity(model: Any) -> Runnable[Any, Any]:
    """The simplest possible `build`: the chain is the model itself."""
    return model


# --- the failover ---------------------------------------------------------------------


async def test_the_primary_answers_when_it_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failover, not a load balancer: a healthy deployment never reaches the alternate,
    so answers stay consistent."""
    _providers(monkeypatch, _Model("openai"), (_Model("gemini"),))
    assert await factory.resilient(_identity).ainvoke({}) == "openai"


async def test_the_alternate_answers_when_the_primary_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _providers(monkeypatch, _Model("openai", fails=True), (_Model("gemini"),))
    assert await factory.resilient(_identity).ainvoke({}) == "gemini"


async def test_with_no_alternate_the_failure_is_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One provider configured is the ordinary case, not a degraded one -- the chain is
    used unwrapped and raises exactly as it did before any of this existed."""
    _providers(monkeypatch, _Model("openai", fails=True), ())
    with pytest.raises(RuntimeError):
        await factory.resilient(_identity).ainvoke({})


def test_no_alternate_means_no_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _Model("openai")
    _providers(monkeypatch, primary, ())
    assert factory.resilient(_identity) is primary


def test_the_chain_is_rebuilt_per_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build` is handed a model and returns a finished chain, so binding, structured
    output and parsing happen per provider. Attaching a fallback to a bare model instead
    would give the alternate the primary's tool schema."""
    built: list[Any] = []

    def _build(model: Any) -> Runnable[Any, Any]:
        built.append(model)
        return model

    primary, alternate = _Model("openai"), _Model("gemini")
    _providers(monkeypatch, primary, (alternate,))
    factory.resilient(_build)
    assert built == [primary, alternate]


# --- what may and may not be fallen back to ----------------------------------------------


def test_an_unconfigured_provider_is_never_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fallback that provisions itself is a bill the operator did not agree to."""
    monkeypatch.setattr(settings, "AI_PROVIDER", "openai")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(factory, "build_chat_model", lambda provider: _Model(provider))
    assert _REAL_FALLBACKS.__wrapped__() == ()


def test_a_configured_second_provider_is_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "openai")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "a-key")
    monkeypatch.setattr(factory, "build_chat_model", lambda provider: _Model(provider))
    assert len(_REAL_FALLBACKS.__wrapped__()) == 1


def test_the_setting_turns_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "openai")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "a-key")
    monkeypatch.setattr(settings, "CHAT_PROVIDER_FALLBACK", False)
    monkeypatch.setattr(factory, "build_chat_model", lambda provider: _Model(provider))
    assert _REAL_FALLBACKS.__wrapped__() == ()


def test_the_primary_is_never_listed_as_its_own_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "openai")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "a-key")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "a-key")
    monkeypatch.setattr(factory, "build_chat_model", lambda provider: _Model(provider))
    names = [model.invoke({}) for model in _REAL_FALLBACKS.__wrapped__()]
    assert names == ["gemini"]


def test_the_chat_factory_cannot_reach_embeddings() -> None:
    """The enforcement, not a promise: vectors from two providers are not comparable --
    Gemini's 768 dims are zero-padded to 1536 and OpenAI's are natively 1536 -- so a
    query embedded by the alternate would rank against a space it was not drawn from.
    Embeddings live on `AIProvider`, which this module does not import.
    """
    source = Path(factory.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]  # skip the module docstring, which explains why
    assert "embed" not in body.lower()


def test_a_fallback_wrapper_is_not_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Why `get_chat_model` returns a bare model and the fallbacks are applied after.

    `with_fallbacks` produces a `RunnableWithFallbacks`, which has neither `bind_tools`
    nor `with_structured_output` -- so wrapping inside the factory would silently take
    away the two features the tool lane and the planner are built on, and the failure
    would surface as an AttributeError inside a reply rather than here.
    """
    _providers(monkeypatch, _Model("openai"), (_Model("gemini"),))
    wrapped = factory.resilient(_identity)
    assert not hasattr(wrapped, "bind_tools")
    assert not hasattr(wrapped, "with_structured_output")
