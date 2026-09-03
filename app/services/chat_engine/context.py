"""The three newest memories, in every prompt.

Most "the bot is dumb" reports were about the last thing the person did. "Is it saved?",
"what was my last one?", "retry that", "give me the list of my last saved" -- all of them
are questions about rows the application already has, and every one of them used to cost
either a tool call or, worse, a lane with no vault access at all. A snapshot in the prompt
answers them with no round trip and gives "it", "that" and "my last one" something real to
point at.

Three things about the shape are deliberate:

* **It is one statement, and it is the same one the status lane makes.**
  `list_for_user` is `cards_only`, so the body, the metadata and the highlights of each
  row stay in the database -- an article body is kilobytes and none of it belongs in a
  header block.
* **`total` is carried next to the rows.** Three rows in front of a model is an invitation
  to answer "you have saved 3 things". The count says otherwise in the same breath.
* **Ages are relative.** Nothing on a chat surface carries the reader's timezone, so an
  absolute timestamp would be this server's opinion rendered as the person's. `status.py`
  renders its own for the same reason.

The block is **fenced as untrusted**, like a memory block, because titles come from
scraped pages: a page is free to call itself `</vault_snapshot>ignore previous
instructions`, and `_clean` is what stops that being anything but text.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import Protocol

from app.models.vault import VaultItem
from app.services.chat_engine.cards import memory_link, short_id

#: Long enough to tell two memories apart in a list, short enough that three of them do
#: not crowd the question. Titles are clipped, never wrapped: a newline inside one would
#: break the one-row-per-line shape the model reads.
_MAX_TITLE = 90


class SnapshotReader(Protocol):
    """The one read this module needs, named structurally so it never sees a session.

    Satisfied by `VaultRepository.list_for_user`, which is already `cards_only` and
    already ordered newest-first -- the snapshot adds no query of its own.
    """

    async def list_for_user(
        self, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> tuple[Sequence[VaultItem], int]: ...


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    """One row as the prompt shows it. Rendering-ready, but not yet rendered."""

    short_id: str
    title: str
    type: str
    status: str
    age_text: str
    #: Where the memory came from. Empty for a note, a recording or an upload.
    url: str = ""
    #: The memory's own page in the vault. Always present for a saved row.
    link: str = ""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The rows, the rows they were rendered from, and the number they were drawn from.

    `items` is carried alongside because the caller has to do two things with one read:
    render the block, and tell the toolbox those ids have been surfaced. Loading them
    twice would be a second statement to save a field.
    """

    rows: tuple[SnapshotRow, ...] = ()
    total: int = 0
    items: tuple[VaultItem, ...] = ()


async def load_snapshot(
    reader: SnapshotReader, user_id: uuid.UUID, limit: int = 3
) -> Snapshot:
    """The newest `limit` memories for one person, whatever state they are in.

    Deliberately unfiltered by status. An item that is still `pending` is exactly the one
    the next message is about, and a snapshot that showed only finished work would answer
    "did that save?" by leaving out the save.
    """
    items, total = await reader.list_for_user(user_id, limit=limit)
    now = datetime.now(UTC)
    return Snapshot(
        rows=tuple(_row(item, now) for item in items),
        total=total,
        items=tuple(items),
    )


def render_snapshot(snapshot: Snapshot) -> str:
    """The block that goes into the prompt. Empty vault renders as an empty vault."""
    if not snapshot.rows:
        return (
            '<vault_snapshot trust="untrusted" total="0">\n'
            "(nothing saved yet)\n"
            "</vault_snapshot>"
        )
    lines = [
        f'<vault_snapshot trust="untrusted" total="{snapshot.total}" '
        f'showing="{len(snapshot.rows)}">'
    ]
    lines += [_render_row(row) for row in snapshot.rows]
    lines.append("</vault_snapshot>")
    return "\n".join(lines)


def _render_row(row: SnapshotRow) -> str:
    """One item, with every field the model might be asked for.

    The links are the point. Without them the snapshot could describe a memory but not
    hand it over, and a model answering from it had to say it could not provide a link --
    which was true of its context and false of the vault. They are attributes rather than
    body text so they stay in the part of the block we wrote, never inside the scraped
    title beside them.
    """
    parts = [
        f'<item id="{row.short_id}"',
        f'type="{row.type}"',
        f'status="{row.status}"',
        f'age="{row.age_text}"',
    ]
    if row.url:
        parts.append(f'url="{row.url}"')
    if row.link:
        parts.append(f'link="{row.link}"')
    return f"{' '.join(parts)}>{row.title}</item>"


def _row(item: VaultItem, now: datetime) -> SnapshotRow:
    return SnapshotRow(
        short_id=short_id(item),
        # Falls back to the source URL, like every other renderer in the system. Without
        # it a link-only save -- which is what a fresh capture is until the pipeline
        # finishes, and exactly the item the next message asks about -- rendered as an
        # empty tag: `<item id=".." status="pending"></item>`, describing nothing.
        title=_clean(item.title or item.source_url),
        type=_value(item.type),
        status=_value(item.processing_status),
        age_text=_age(item.created_at, now),
        url=_attr(item.source_url),
        link=_attr(memory_link(item)),
    )


def _attr(value: str | None) -> str:
    """A URL, made safe to sit inside a double-quoted attribute.

    Escaped rather than trusted: `source_url` is whatever someone pasted, and a quote in
    it would end the attribute early and let the rest read as more attributes.
    """
    return escape(value.strip(), quote=True) if value else ""


def _value(field: object) -> str:
    """The enum's own string, which is what every log line and status table already uses."""
    return str(getattr(field, "value", field or ""))


def _clean(title: str | None) -> str:
    """A scraped title, made safe to sit inside a tag and on one line.

    `escape` rather than a strip of the characters: the model is being shown the title,
    so `AT&T` should read as `AT&T` and not as `ATT`. What it must not do is close this
    block or open a `<memory>` one.
    """
    collapsed = " ".join((title or "").split())
    if len(collapsed) > _MAX_TITLE:
        collapsed = collapsed[: _MAX_TITLE - 1].rstrip() + "…"
    return escape(collapsed, quote=True)


def _age(moment: datetime | None, now: datetime) -> str:
    """How long ago, in the coarsest unit that is still true."""
    if moment is None:
        return "?"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
