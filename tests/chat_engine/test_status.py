"""The lane that answers from the row, not from the model.

Two things are being pinned here and they are worth separating. The first is the
wording: every processing state has to produce a sentence that is *true* about that
state, because this is the one answer a user acts on ("it failed, send it again"). The
second is that no provider is reachable from this path at all -- the fake reader below is
the only collaborator, and a test that needed a model would be the regression.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine import status
from app.services.chat_engine.types import TextBlock

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _item(
    state: ProcessingStatus = ProcessingStatus.completed,
    *,
    title: str | None = "BellaVita White Oud",
    age: timedelta = timedelta(minutes=2),
    source_url: str | None = None,
) -> VaultItem:
    return VaultItem(
        user_id=_USER,
        type=ContentType.article,
        title=title,
        source_url=source_url,
        processing_status=state,
        created_at=_NOW - age,
    )


class FakeReader:
    """The whole collaboration: one scoped read, recorded."""

    def __init__(self, items: Sequence[VaultItem]) -> None:
        self.items = items
        self.calls: list[tuple[uuid.UUID, int]] = []

    async def recent_saves(
        self, user_id: uuid.UUID, limit: int
    ) -> tuple[Sequence[VaultItem], int]:
        self.calls.append((user_id, limit))
        return self.items, len(self.items)


# --- what each state says -------------------------------------------------------------


def test_a_completed_save_is_reported_as_saved() -> None:
    text = status.describe([_item()], "is it saved?", now=_NOW)
    assert "Saved" in text
    assert "BellaVita White Oud" in text


def test_an_unfinished_save_says_so_rather_than_yes_or_no() -> None:
    """The race the screenshot showed: asked between the acknowledgement and the worker.

    "I can't check" and "yes, saved" are both wrong here; "still processing" is the
    only true answer, and it is the one a person can wait on.
    """
    text = status.describe([_item(ProcessingStatus.processing)], "is it saved?", now=_NOW)
    assert "still processing" in text


def test_a_pending_item_reads_the_same_as_a_processing_one() -> None:
    """Queued and running are one answer from the sender's side: it arrived, it is not
    done. The distinction belongs to the sweeper, not to the reply."""
    queued = status.describe([_item(ProcessingStatus.pending)], "", now=_NOW)
    running = status.describe([_item(ProcessingStatus.processing)], "", now=_NOW)
    assert queued == running


def test_a_failed_save_is_reported_as_failed() -> None:
    text = status.describe([_item(ProcessingStatus.failed)], "did it save?", now=_NOW)
    assert "didn't go through" in text


def test_a_skipped_item_is_saved_but_says_there_is_no_summary() -> None:
    """Stored and downloadable, never sent to a model. "Failed" would suggest the file
    is gone and it is not; a bare "saved" would promise a summary that never arrives."""
    text = status.describe([_item(ProcessingStatus.skipped)], "", now=_NOW)
    assert "Saved" in text
    assert "no summary" in text


def test_an_empty_vault_says_nothing_is_saved_yet() -> None:
    assert "Nothing saved yet" in status.describe([], "is it saved?", now=_NOW)


def test_the_processing_error_is_never_echoed() -> None:
    """It is scrubbed on the way into the database and is still a provider's own words
    about our infrastructure. The user needs the outcome, not the stack."""
    item = _item(ProcessingStatus.failed)
    item.processing_error = "apify 403 for token=abcdef"
    text = status.describe([item], "", now=_NOW)
    assert "apify" not in text.lower()
    assert "abcdef" not in text


# --- the rest of the page -------------------------------------------------------------


def test_other_unfinished_saves_are_counted() -> None:
    """Someone who sent three things and asks "did that save?" means all of them."""
    items = [_item(), _item(ProcessingStatus.processing), _item(ProcessingStatus.pending)]
    assert "2 more still processing" in status.describe(items, "", now=_NOW)


def test_the_newest_item_is_not_also_counted_as_more() -> None:
    items = [_item(ProcessingStatus.processing)]
    assert "more still processing" not in status.describe(items, "", now=_NOW)


def test_finished_items_further_down_are_not_counted() -> None:
    items = [_item(), _item(), _item()]
    assert "more still processing" not in status.describe(items, "", now=_NOW)


# --- naming the memory ----------------------------------------------------------------


def test_an_untitled_item_falls_back_to_its_url() -> None:
    item = _item(title=None, source_url="https://example.com/reel/1")
    assert "https://example.com/reel/1" in status.describe([item], "", now=_NOW)


def test_an_item_with_neither_title_nor_url_still_produces_a_sentence() -> None:
    """A capture asked about before enrichment has run has no title yet."""
    item = _item(ProcessingStatus.processing, title=None)
    assert "your last save" in status.describe([item], "", now=_NOW)


def test_a_long_title_is_clipped() -> None:
    item = _item(title="x" * 400)
    assert len(status.describe([item], "", now=_NOW)) < 200


# --- when ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(seconds=5), "just now"),
        (timedelta(minutes=20), "20 min ago"),
        (timedelta(hours=5), "5h ago"),
        (timedelta(days=3), "3d ago"),
    ],
)
def test_times_are_relative(age: timedelta, expected: str) -> None:
    """Nothing in a chat surface carries the sender's timezone, so an absolute time
    would be this server's opinion rendered as the user's."""
    assert expected in status.describe([_item(age=age)], "", now=_NOW)


def test_a_naive_timestamp_does_not_raise() -> None:
    """`timestamptz` round-trips as aware, but a fixture can hand over a naive one and
    this lane's whole job is to be the thing that still works."""
    item = _item()
    item.created_at = datetime(2026, 9, 1, 11, 58)
    assert "min ago" in status.describe([item], "", now=_NOW) or "just now" in status.describe(
        [item], "", now=_NOW
    )


def test_a_missing_timestamp_does_not_raise() -> None:
    item = _item()
    item.created_at = None  # type: ignore[assignment]
    assert status.describe([item], "", now=_NOW)


# --- language --------------------------------------------------------------------------


def test_a_bengali_question_is_answered_in_bengali() -> None:
    """There is no model call in this lane, so nothing in the loop could translate the
    reply. A script-keyed table is the only honest way to answer in the asker's script."""
    text = status.describe([_item()], "সেভ হয়েছে?", now=_NOW)
    assert "সেভ হয়েছে" in text


def test_an_unlisted_script_falls_back_to_english() -> None:
    """Plain English beats a machine-translated sentence that reads as broken."""
    assert "Saved" in status.describe([_item()], "保存されましたか", now=_NOW)


def test_the_memory_title_does_not_decide_the_language() -> None:
    """An English question about a Bengali note is answered in English: the script that
    matters is the one the person wrote in."""
    text = status.describe([_item(title="আমার নোট")], "is it saved?", now=_NOW)
    assert "Saved" in text


# --- the read itself ---------------------------------------------------------------------


async def test_reply_reads_only_the_callers_own_user() -> None:
    """Nothing in the message chooses which rows are read -- no id, no filter -- so this
    lane has no object to reference insecurely."""
    reader = FakeReader([_item()])
    await status.reply(reader, _USER, "is it saved?")
    assert reader.calls == [(_USER, status.LOOKBACK)]


async def test_reply_returns_prose_and_not_markup() -> None:
    reply = await status.reply(FakeReader([_item()]), _USER, "is it saved?")
    assert len(reply.blocks) == 1
    block = reply.blocks[0]
    assert isinstance(block, TextBlock)
    assert "<" not in block.text
