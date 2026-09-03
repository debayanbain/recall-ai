"""Deleting a memory: what goes, what stays, and what must stop finding it.

`VaultItem.deleted_at` and the `deleted_at IS NULL` filter on every read were added long
before anything wrote the column -- `VaultRepository.delete` did a hard `session.delete()`
-- so the filters could never match and the soft-delete path had never run. That is the
gap these cover.

The design decision worth arguing with, if anyone comes to change it: the tombstone is
**scrubbed**. It keeps the id, the owner, the kind and the timestamps, and nothing else. A
tombstone is for referential integrity and for explaining a gap; it is not a copy of a
memory somebody asked to be rid of, and "we kept it in case you want it back" is not a
promise this product made.

Needs a real PostgreSQL with pgvector; skipped otherwise, like every DB-backed test here.
"""
from __future__ import annotations

import uuid

from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.base import ContentType, ProcessingStatus
from app.models.user import User
from app.models.vault import VaultChunk, VaultItem
from app.repositories.vault import VaultRepository
from app.services.vault_service import VaultService


async def _seed(session: AsyncSession, owner: User, *, chunks: int = 1) -> VaultItem:
    item = VaultItem(
        user_id=owner.id,
        type=ContentType.article,
        title="Redis persistence",
        summary="RDB snapshots versus the append-only file.",
        content="The append-only file is rewritten when it doubles in size.",
        source_url="https://example.com/redis",
        thumbnail_url="https://example.com/thumb.png",
        language="en",
        storage_key="users/a/b/c.pdf",
        file_name="redis.pdf",
        file_size=1234,
        mime_type="application/pdf",
        ai_tags=["redis", "databases"],
        ai_highlights=["The append-only file is rewritten when it doubles in size."],
        ai_label="Redis AOF rewriting",
        ai_category="Technology",
        item_metadata={"source": "telegram", "telegram_chat_id": "4242"},
        processing_status=ProcessingStatus.completed,
        processing_error=None,
    )
    session.add(item)
    await session.flush()
    for index in range(chunks):
        session.add(
            VaultChunk(
                vault_item_id=item.id,
                user_id=owner.id,
                chunk_index=index,
                content="The append-only file is rewritten.",
                embedding=[0.1] * settings.EMBEDDING_DIM,
            )
        )
    await session.commit()
    return item


class _Storage:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


# --- the row ---------------------------------------------------------------------------


async def test_the_row_survives_as_a_tombstone(session: AsyncSession, alice: User) -> None:
    """Not a hard delete: `space_items` and any future audit still have something to point at."""
    item = await _seed(session, alice)
    service = VaultService(VaultRepository(session), _Storage())  # type: ignore[arg-type]

    assert await service.delete(item.id, alice.id) is True
    await session.commit()

    raw = await session.get(VaultItem, item.id)
    assert raw is not None
    assert raw.deleted_at is not None


async def test_everything_the_memory_held_is_scrubbed(
    session: AsyncSession, alice: User
) -> None:
    """The person asked for it to be gone. A tombstone that still holds the text is not gone."""
    item = await _seed(session, alice)
    service = VaultService(VaultRepository(session), _Storage())  # type: ignore[arg-type]

    await service.delete(item.id, alice.id)
    await session.commit()

    raw = await session.get(VaultItem, item.id)
    assert raw is not None
    assert raw.title is None
    assert raw.summary is None
    assert raw.content is None
    assert raw.source_url is None
    assert raw.thumbnail_url is None
    assert raw.ai_tags == []
    assert raw.ai_highlights == []
    assert raw.ai_label is None
    assert raw.ai_category is None
    assert raw.item_metadata == {}
    assert raw.storage_key is None
    assert raw.file_name is None
    assert raw.mime_type is None
    # What a tombstone is allowed to keep: enough to explain a gap, not to reconstruct.
    assert raw.user_id == alice.id
    assert raw.type is ContentType.article
    assert raw.created_at is not None


async def test_the_chunks_go_with_it(session: AsyncSession, alice: User) -> None:
    """They carry the same words *and* the vector drawn from them.

    Leaving them keeps deleted text searchable in the one index built to find it. The
    join in `search_semantic` also hides it, but defence in depth is not a reason to
    retain the content.
    """
    item = await _seed(session, alice, chunks=3)
    service = VaultService(VaultRepository(session), _Storage())  # type: ignore[arg-type]

    await service.delete(item.id, alice.id)
    await session.commit()

    remaining = await session.exec(
        select(func.count())
        .select_from(VaultChunk)
        .where(col(VaultChunk.vault_item_id) == item.id)
    )
    assert remaining.one() == 0


async def test_the_stored_object_is_removed(session: AsyncSession, alice: User) -> None:
    """The key is read before the scrub clears it, or the bytes would be orphaned."""
    item = await _seed(session, alice)
    storage = _Storage()
    service = VaultService(VaultRepository(session), storage)  # type: ignore[arg-type]

    await service.delete(item.id, alice.id)

    assert storage.deleted == ["users/a/b/c.pdf"]


# --- and every way of reading it back ---------------------------------------------------


async def test_a_deleted_item_reads_as_missing(session: AsyncSession, alice: User) -> None:
    """`get` used not to filter `deleted_at`, so a tombstone stayed fully readable by id."""
    item = await _seed(session, alice)
    repo = VaultRepository(session)
    await VaultService(repo, _Storage()).delete(item.id, alice.id)  # type: ignore[arg-type]

    assert await repo.get(item.id, alice.id) is None


async def test_the_worker_will_not_process_it_either(
    session: AsyncSession, alice: User
) -> None:
    """An item deleted while queued must not be summarised, tagged or replied about.

    `get_unscoped` skips the *tenant* check because the worker has no request user.
    Skipping the deletion check was never part of that.
    """
    item = await _seed(session, alice)
    repo = VaultRepository(session)
    await VaultService(repo, _Storage()).delete(item.id, alice.id)  # type: ignore[arg-type]

    assert await repo.get_unscoped(item.id) is None


async def test_it_leaves_the_listings(session: AsyncSession, alice: User) -> None:
    item = await _seed(session, alice)
    repo = VaultRepository(session)
    await VaultService(repo, _Storage()).delete(item.id, alice.id)  # type: ignore[arg-type]

    rows, total = await repo.list_for_user(alice.id)
    assert [row.id for row in rows] == []
    assert total == 0
    filtered, _ = await repo.list_filtered(alice.id)
    assert list(filtered) == []


async def test_deleting_twice_is_not_an_error(session: AsyncSession, alice: User) -> None:
    """The second one answers False, the same as deleting something that never existed."""
    item = await _seed(session, alice)
    service = VaultService(VaultRepository(session), _Storage())  # type: ignore[arg-type]

    assert await service.delete(item.id, alice.id) is True
    assert await service.delete(item.id, alice.id) is False


async def test_the_same_link_can_be_saved_again_afterwards(
    session: AsyncSession, alice: User
) -> None:
    """Dedupe looks for a *live* item, so a deleted link is not a link you cannot re-save."""
    item = await _seed(session, alice)
    repo = VaultRepository(session)
    await VaultService(repo, _Storage()).delete(item.id, alice.id)  # type: ignore[arg-type]

    assert await repo.get_by_source_url(alice.id, "https://example.com/redis") is None


async def test_another_users_item_is_untouched(session: AsyncSession, alice: User) -> None:
    """Deletion is scoped like every other write: `get` is the check, and it is the only one."""
    item = await _seed(session, alice)
    service = VaultService(VaultRepository(session), _Storage())  # type: ignore[arg-type]

    assert await service.delete(item.id, uuid.uuid4()) is False
    await session.commit()

    raw = await session.get(VaultItem, item.id)
    assert raw is not None and raw.deleted_at is None and raw.title == "Redis persistence"
