"""Chat-model selection, mirroring `app/ai/factory.py`.

Same setting (`AI_PROVIDER`), same lru_cache, same "unsupported provider is an error, not
a fallback" rule for *selection* -- an `AI_PROVIDER` this module cannot build raises,
because answering a user with a model they did not choose is not a recovery.

**Failing over during an outage is a different question, and the answer is different
too.** When the operator has configured a second provider -- both keys present, both paid
for -- a 500 from the first one is not a reason to lose the reply. `resilient()` wraps
whatever chain the caller builds so the same chain is rebuilt on the alternate model and
tried once. Three limits on it, and each is load-bearing:

* **Only the chat model, never embeddings.** Vectors from two providers are not
  comparable: Gemini's 768 dims are zero-padded to 1536 and OpenAI's are natively 1536,
  so a query embedded by the alternate would rank against a space it was not drawn from
  -- plausible ordering over noise, with nothing to notice. Embeddings live on
  `AIProvider` and this module cannot reach them, which is the enforcement.
* **Only when the other provider is genuinely configured.** No key, no fallback. A
  fallback that provisions itself is a bill the operator did not agree to.
* **It is a failover, not a load balancer.** The primary is always tried first, so a
  healthy deployment never touches the alternate and the answers stay consistent.

A fallback firing is visible without new logging: `UsageLogger` records `model=` on every
call, so a run answered by the alternate says so in the same line that reports its cost.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Literal, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from app.core.config import settings

# Low but not zero: the answer is grounded in retrieved text, so creativity is a liability,
# while a completely deterministic model phrases every "nothing found" identically.
_TEMPERATURE = 0.2
_TIMEOUT = 30.0
_MAX_RETRIES = 2

#: Hard ceiling on a reply, in tokens. The prompts ask for under four sentences and that
#: is what shapes the answer; this is the backstop for when the model ignores them, since
#: replies land on a phone screen and a wall of text is unreadable there and paid for by
#: the token. Set generously above four sentences (~100 tokens) so a normal reply is
#: never cut off mid-word -- a truncated answer is worse than a long one.
#:
#: **It has to sit above `RECALL_ANSWER_MAX_CHARS`, and at 256 it did not.** That guard
#: allows 1500 characters, roughly 375 tokens, so the model could not reach the length it
#: was permitted: measured on a real "give me those links" turn, gpt-4o spent all 256 and
#: was cut off mid-URL. A truncated URL is worse than a long answer twice over -- the
#: guard then fails to match it against the allowlist and replaces it with
#: `[link omitted]`.
#:
#: It is also the budget a *reasoning* model spends before it writes anything. gpt-5-mini
#: fails outright at 256 with "max_tokens or model output limit was reached", having spent
#: the lot on reasoning tokens. Anything in that family needs room to think and then
#: answer, which is the other reason this is not tight.
#:
#: The kwarg is named differently by each provider, which is why this lives here: the
#: factory is the only place that knows which provider is configured. It applies to the
#: planner's structured call too, which needs roughly sixty tokens, so the headroom is
#: ample there as well.
_MAX_OUTPUT_TOKENS = 512


def chat_available() -> bool:
    """True when the configured provider has a key. Checked before any chat feature."""
    if settings.AI_PROVIDER == "openai":
        return bool(settings.OPENAI_API_KEY)
    if settings.AI_PROVIDER == "gemini":
        return bool(settings.GEMINI_API_KEY)
    return False


def _has_key(provider: str) -> bool:
    if provider == "openai":
        return bool(settings.OPENAI_API_KEY)
    if provider == "gemini":
        return bool(settings.GEMINI_API_KEY)
    return False


def build_chat_model(provider: str, model: str | None = None) -> BaseChatModel:
    """One provider's chat model. Raises for a provider this module cannot build.

    `model` overrides the provider's configured model id and nothing else -- same key,
    same temperature, same timeouts. It exists so the agent loop can run on a different
    model without a second copy of this construction, which is how one of the two ends up
    with a timeout nobody meant to change.
    """
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model or settings.OPENAI_TEXT_MODEL,
            api_key=settings.OPENAI_API_KEY,
            temperature=_TEMPERATURE,
            timeout=_TIMEOUT,
            max_retries=_MAX_RETRIES,
            max_tokens=_MAX_OUTPUT_TOKENS,
        )
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model or settings.GEMINI_TEXT_MODEL,
            google_api_key=settings.GEMINI_API_KEY,
            temperature=_TEMPERATURE,
            timeout=_TIMEOUT,
            max_retries=_MAX_RETRIES,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        )
    raise ValueError(f"No chat model for AI_PROVIDER={provider!r}")


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    """The configured provider's model. Still a `BaseChatModel`, deliberately.

    `with_fallbacks` returns a `RunnableWithFallbacks`, which has no `bind_tools` and no
    `with_structured_output` -- so wrapping here would quietly remove the two features
    the tool lane and the planner are built on. Fallbacks are applied by `resilient()`
    *after* the caller has bound what it needs.
    """
    return build_chat_model(settings.AI_PROVIDER)


@lru_cache(maxsize=1)
def get_agent_model() -> BaseChatModel:
    """The model that drives the agent loop. Its own setting, for its own cost shape.

    The older lanes spend one model call per question; the loop spends one per round, so
    which model runs it matters more here and is worth setting separately. Empty means
    "the same one everything else uses", which is the right default -- a second model id
    nobody set is a second thing to keep in step.

    Bare, like `get_chat_model`, and for the same reason: `with_fallbacks` returns a
    `RunnableWithFallbacks`, which has neither `bind_tools` nor `with_structured_output`,
    and this lane is built on both.
    """
    configured = settings.AGENT_CHAT_MODEL.strip()
    if not configured:
        return get_chat_model()
    return build_chat_model(settings.AI_PROVIDER, configured)


@lru_cache(maxsize=1)
def fallback_models() -> tuple[BaseChatModel, ...]:
    """Every configured provider other than the primary, in a fixed order.

    Empty when the operator has configured only one, which is the ordinary case and not
    a degraded one: the chain is then used unwrapped and behaves exactly as before.
    """
    if not settings.CHAT_PROVIDER_FALLBACK:
        return ()
    providers: tuple[Literal["openai", "gemini"], ...] = ("openai", "gemini")
    models = []
    for provider in providers:
        if provider == settings.AI_PROVIDER or not _has_key(provider):
            continue
        try:
            models.append(build_chat_model(provider))
        except (ValueError, ImportError):
            # A provider whose package is not installed is not an error here -- it is
            # simply not available to fall back to.
            continue
    return tuple(models)


_Out = TypeVar("_Out")


def resilient(
    build: Callable[[BaseChatModel], Runnable[dict[str, object], _Out]],
) -> Runnable[dict[str, object], _Out]:
    """The caller's chain on the primary model, backed by the same chain on the others.

    `build` is handed a model and returns a finished chain, so binding, structured output
    and parsing all happen per model -- an alternate that needed a different tool schema
    or a different parser would get one, and none of that is expressible if the fallback
    is attached to a bare model instead.
    """
    primary = build(get_chat_model())
    alternates = [build(model) for model in fallback_models()]
    if not alternates:
        return primary
    return primary.with_fallbacks(alternates)
