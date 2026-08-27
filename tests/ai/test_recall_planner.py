"""Query planning: the free routing decision, and distrusting the model's output.

`looks_like_question` runs on every plain-text message and must cost nothing, so it is
pure string work. What it decides is which *lane of chat* a message takes -- retrieval
over the vault, or plain conversation. It no longer decides whether anything is saved:
capture is explicit now (a link, a file, or `/note`), so a misroute here costs one
oddly-shaped reply rather than a stray memory or a lost one.

`plan` output reaches a SQL filter, so it is validated like any other input even though a
model produced it.
"""
from __future__ import annotations

from app.ai.chat.planner import (
    MemoryQuery,
    looks_like_question,
    resolved_content_types,
)
from app.models.base import ContentType


def test_question_marks_are_questions() -> None:
    assert looks_like_question("any cooking videos?")
    assert looks_like_question("did I save that reel?")


def test_retrieval_phrasing_without_a_question_mark() -> None:
    for text in (
        "show my SaaS ideas",
        "what did I save this week",
        "find me the fasting reel",
        "remind me what I saved about FastAPI",
        "any pasta recipes",
    ):
        assert looks_like_question(text), text


def test_statements_take_the_conversational_lane() -> None:
    """Not questions about the vault, so retrieval would return nothing and say so."""
    for text in (
        "hi",
        "thanks!",
        "idea: a bot that remembers things",
        "great article on rust ownership",
        "buy oat milk",
        "Whatsapp the team about Friday",
    ):
        assert not looks_like_question(text), text


def test_commands_and_blanks_are_never_questions() -> None:
    assert not looks_like_question("/recent")
    assert not looks_like_question("   ")


def test_hallucinated_content_types_are_dropped() -> None:
    query = MemoryQuery(
        search_text="pasta", content_types=["youtube", "podcast", "hologram"]
    )
    valid = {t.value for t in ContentType}
    query.content_types = [t for t in query.content_types if t in valid]
    assert query.content_types == ["youtube"]
    assert resolved_content_types(query) == [ContentType.youtube]


def test_no_content_types_means_no_filter() -> None:
    assert resolved_content_types(MemoryQuery(search_text="pasta")) is None
