"""Turning "any cooking videos from last week?" into query parameters.

One structured model call. `with_structured_output` binds the schema as a tool call, so
the model returns a validated `MemoryQuery` rather than prose we have to parse -- which
is what `_parse_tags` in the providers exists to cope with, and what this avoids having
to repeat.

The output is still treated as untrusted: `content_types` is checked against the real
enum and `days` is bounded, because a hallucinated value would otherwise reach a SQL
filter. A failed call degrades to searching the raw question with no filters, which is a
worse answer rather than no answer.
"""
from __future__ import annotations

import re

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.ai.chat.factory import get_chat_model
from app.ai.chat.usage import UsageLogger
from app.core.logging import get_logger
from app.models.base import ContentType

log = get_logger("recall.chat")

_MAX_DAYS = 3650


class MemoryQuery(BaseModel):
    """The parameters a natural-language question maps onto."""

    search_text: str = Field(
        description=(
            "The subject to search for, stripped of time words and filler. "
            "'any cooking videos from last week?' -> 'cooking'. "
            "Empty string if the question is purely about time, e.g. "
            "'what did I save this week?'."
        )
    )
    days: int | None = Field(
        default=None,
        description=(
            "How many days back the question covers, if it mentions a period at all. "
            "'last 5 days' -> 5, 'this week' -> 7, 'last month' -> 30. "
            "Null when no period is mentioned."
        ),
    )
    content_types: list[str] = Field(
        default_factory=list,
        description=(
            "Content kinds the question restricts to, from: youtube, article, pdf, "
            "document, note, instagram, facebook, tiktok, linkedin, voice, image. "
            "A question about 'videos' means youtube, instagram and facebook. "
            "Empty when the question does not restrict by kind."
        ),
    )
    category: str | None = Field(
        default=None,
        description=(
            "One of Technology, Business, Science, Health, Education, Entertainment, "
            "News, Productivity, Finance, Lifestyle -- only if the question names it "
            "outright. Null otherwise."
        ),
    )


_PLANNER_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You convert a person's question about their own saved bookmarks into "
            "search parameters. Extract only what the question actually says; never "
            "invent a time period or a content kind that was not asked for. Today's "
            "date is irrelevant -- express time as a number of days back.",
        ),
        ("human", "{question}"),
    ]
)

# Interrogatives and retrieval verbs. A message that matches none of these is treated as
# something to save. That direction is deliberate: mistaking a question for a note leaves
# a stray note the user can delete, while mistaking a note for a question silently
# discards something they meant to keep.
_QUESTION_STARTERS = (
    "what", "which", "when", "where", "who", "why", "how", "did", "do", "does",
    "is", "are", "was", "were", "can", "could", "have", "has", "any", "anything",
    "show", "find", "search", "list", "remind", "tell", "give", "recall", "look",
)
_QUESTION_PHRASES = (
    "did i save", "do i have", "have i saved", "what did i", "show me", "find me",
    "remind me", "look up", "search for",
)
_WORD_RE = re.compile(r"[a-z']+")


def looks_like_question(text: str) -> bool:
    """Cheap routing decision, made without a model call.

    Runs on every plain-text message, so it must not cost a token. The planner is only
    reached once this returns True.
    """
    stripped = text.strip()
    if not stripped or stripped.startswith("/"):
        return False
    lowered = stripped.lower()
    if lowered.endswith("?"):
        return True
    if any(phrase in lowered for phrase in _QUESTION_PHRASES):
        return True
    words = _WORD_RE.findall(lowered)
    return bool(words) and words[0] in _QUESTION_STARTERS


def _fallback(question: str) -> MemoryQuery:
    return MemoryQuery(search_text=question.strip()[:500])


async def plan(question: str) -> MemoryQuery:
    """Extract query parameters, defensively."""
    try:
        chain = _PLANNER_PROMPT | get_chat_model().with_structured_output(MemoryQuery)
        result = await chain.ainvoke(
            {"question": question}, config={"callbacks": [UsageLogger("planner")]}
        )
    except Exception as exc:  # noqa: BLE001 - a worse search beats no search
        log.warning("recall_planner_failed", error=type(exc).__name__)
        return _fallback(question)

    if not isinstance(result, MemoryQuery):
        log.warning("recall_planner_wrong_shape", got=type(result).__name__)
        return _fallback(question)

    # The model's output is input to a SQL filter, so it is validated like any other.
    valid = {t.value for t in ContentType}
    result.content_types = [t for t in result.content_types if t in valid]
    if result.days is not None and not (1 <= result.days <= _MAX_DAYS):
        result.days = None
    result.search_text = result.search_text.strip()[:500]
    return result


def resolved_content_types(query: MemoryQuery) -> list[ContentType] | None:
    if not query.content_types:
        return None
    return [ContentType(value) for value in query.content_types]
