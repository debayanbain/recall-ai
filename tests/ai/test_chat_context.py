"""What actually reaches the answer model: cards inside the fences, and a hard cap.

The block a memory is rendered into carries its own header (title, category, date, url)
while the card inside carries the item's identity. If those two ever drift apart, one
memory's body is attributed to another memory's title -- a wrong answer that reads
completely plausible. That pairing is the thing most worth pinning here.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from langchain_core.documents import Document

from app.ai.chat.chain import format_context
from app.ai.chat.retriever import to_document
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_BODY = "SECRET-BODY-TEXT that used to be pasted into the prompt. " * 40


def _item(n: int, **overrides: object) -> VaultItem:
    values: dict[str, object] = {
        "user_id": _USER,
        "type": ContentType.article,
        "processing_status": ProcessingStatus.completed,
        "created_at": datetime(2026, 8, 25, tzinfo=UTC),
        "title": f"Title {n}",
        "ai_label": f"Label {n}",
        "summary": f"Summary {n}.",
        "ai_category": "Business",
        "content": _BODY,
        "source_url": f"https://example.com/{n}",
    }
    values.update(overrides)
    return VaultItem(**values)


def test_the_fencing_and_its_headers_are_unchanged() -> None:
    context = format_context([to_document(_item(1))])
    assert context.startswith('<memory id="1" title="Title 1"')
    assert 'category="Business"' in context
    assert 'saved="2026-08-25"' in context
    assert 'url="https://example.com/1"' in context
    assert context.endswith("</memory>")


def test_each_block_holds_that_items_card() -> None:
    """The pairing. A drift here mislabels one memory as another."""
    context = format_context([to_document(_item(n)) for n in range(3)])
    blocks = context.split("\n\n")

    assert len(blocks) == 3
    for n, block in enumerate(blocks):
        assert f'title="Title {n}"' in block
        assert f"Label {n}" in block
        assert f"Summary {n}." in block
        # and nothing from its neighbours
        assert f"Label {n + 1}" not in block


def test_the_item_body_never_reaches_the_prompt() -> None:
    """The point of the change: `content` is the field a card exists to leave out."""
    context = format_context([to_document(_item(n)) for n in range(3)])
    assert "SECRET-BODY-TEXT" not in context


def test_the_budget_drops_whole_blocks_rather_than_truncating_one() -> None:
    """A half-rendered card would still be quoted to the model as if it were complete.

    Summaries here are full length on purpose: forty one-line cards genuinely fit, and a
    cap that never engages would make this test pass while proving nothing.
    """
    long_summary = "Applying to employers in this country works as follows. " * 6
    context = format_context(
        [to_document(_item(n, summary=long_summary)) for n in range(40)]
    )
    blocks = context.split("\n\n")

    assert 0 < len(blocks) < 40
    assert all(b.startswith("<memory id=") and b.endswith("</memory>") for b in blocks)
    # Kept blocks are a prefix, in the retriever's relevance order.
    assert [b.split('id="')[1].split('"')[0] for b in blocks] == [
        str(n) for n in range(1, len(blocks) + 1)
    ]


def test_a_document_without_an_item_still_renders_rather_than_vanishing() -> None:
    """An empty context is the one input the answer prompt has no honest reply to."""
    doc = Document(page_content="a plain excerpt", metadata={"title": "Loose"})
    context = format_context([doc])
    assert "a plain excerpt" in context and 'title="Loose"' in context


def test_no_documents_is_an_empty_string() -> None:
    assert format_context([]) == ""
