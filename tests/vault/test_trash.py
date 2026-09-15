"""Deleting a memory: what it fills, what survives it, and what finally destroys it.

Deleting used to be one step -- scrub the row, drop the chunks, remove the bytes -- so a
mistaken tap was unrecoverable at the instant it happened, and a mistaken tap looks
exactly like a deliberate one. It is two steps now:

* `delete` writes `deleted_at` and stops. Every read in the repository filters that
  column, so the memory leaves the vault, search, chat retrieval, connections and the
  worker immediately, while the row, the chunks and the bucket objects all survive.
* `purge` is the old delete: the scrub, the chunk delete, the connection delete and the
  object delete. It runs when the owner asks for it, or `TRASH_RETENTION_DAYS` later from
  the beat task.

The design decision worth arguing with, if anyone comes to change it: the *purge* is still
a scrub. The window is a delay on an irreversible action, not a promise to keep a copy --
past it, what survives is the id, the owner, the kind and the timestamps, and nothing else.

Needs a real PostgreSQL with pgvector; skipped otherwise, like every DB-backed test here.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

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


def _service(session: AsyncSession, storage: _Storage | None = None) -> VaultService:
    return VaultService(VaultRepository(session), storage or _Storage())  # type: ignore[arg-type]


async def _age_out(session: AsyncSession, item: VaultItem, *, days: int) -> None:
    """Backdate a trashed row so the sweep sees it as expired."""
    raw = await session.get(VaultItem, item.id)
    assert raw is not None
    raw.deleted_at = datetime.now(UTC) - timedelta(days=days)
    session.add(raw)
    await session.commit()


# --- delete fills the trash -------------------------------------------------------------


async def test_delete_keeps_everything_the_memory_held(
    session: AsyncSession, alice: User
) -> None:
    """The row is hidden, not emptied. This is the whole difference the trash makes."""
    item = await _seed(session, alice)
    storage = _Storage()

    assert await _service(session, storage).delete(item.id, alice.id) is True
    await session.commit()

    raw = await session.get(VaultItem, item.id)
    assert raw is not None
    assert raw.deleted_at is not None
    assert raw.purged_at is None
    assert raw.title == "Redis persistence"
    assert raw.content is not None
    assert raw.ai_tags == ["redis", "databases"]
    assert raw.storage_key == "users/a/b/c.pdf"
    # The bytes stay too: a restore that returned a file name with nothing behind it is
    # not a restore.
    assert storage.deleted == []


async def test_the_chunks_survive_the_trash(session: AsyncSession, alice: User) -> None:
    """They carry the vector a restore has to put back.

    Re-deriving them would mean re-embedding, so a restore would cost money and could
    quietly fail. They are already invisible -- `search_semantic` joins the item and
    filters `deleted_at`.
    """
    item = await _seed(session, alice, chunks=3)
    await _service(session).delete(item.id, alice.id)
    await session.commit()

    remaining = await session.exec(
        select(func.count())
        .select_from(VaultChunk)
        .where(col(VaultChunk.vault_item_id) == item.id)
    )
    assert remaining.one() == 3


async def test_a_trashed_item_reads_as_missing(session: AsyncSession, alice: User) -> None:
    """`get` filters `deleted_at`, so the memory is gone from every ordinary read."""
    item = await _seed(session, alice)
    repo = VaultRepository(session)
    await _service(session).delete(item.id, alice.id)

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
    await _service(session).delete(item.id, alice.id)

    assert await repo.get_unscoped(item.id) is None


async def test_it_leaves_the_listings(session: AsyncSession, alice: User) -> None:
    item = await _seed(session, alice)
    repo = VaultRepository(session)
    await _service(session).delete(item.id, alice.id)

    rows, total = await repo.list_for_user(alice.id)
    assert [row.id for row in rows] == []
    assert total == 0
    filtered, _ = await repo.list_filtered(alice.id)
    assert list(filtered) == []


async def test_deleting_twice_is_not_an_error(session: AsyncSession, alice: User) -> None:
    """The second one answers False, the same as deleting something that never existed."""
    item = await _seed(session, alice)
    service = _service(session)

    assert await service.delete(item.id, alice.id) is True
    assert await service.delete(item.id, alice.id) is False


async def test_the_same_link_can_be_saved_again_afterwards(
    session: AsyncSession, alice: User
) -> None:
    """Dedupe looks for a *live* item, so a deleted link is not a link you cannot re-save."""
    item = await _seed(session, alice)
    repo = VaultRepository(session)
    await _service(session).delete(item.id, alice.id)

    assert await repo.get_by_source_url(alice.id, "https://example.com/redis") is None


async def test_another_users_item_is_untouched(session: AsyncSession, alice: User) -> None:
    """Deletion is scoped like every other write: `get` is the check, and it is the only one."""
    item = await _seed(session, alice)

    assert await _service(session).delete(item.id, uuid.uuid4()) is False


# --- the trash listing ------------------------------------------------------------------


async def test_the_trash_lists_what_can_come_back(
    session: AsyncSession, alice: User
) -> None:
    item = await _seed(session, alice)
    service = _service(session)
    await service.delete(item.id, alice.id)
    await session.commit()

    rows, total = await service.list_trash(alice.id, limit=20, offset=0)

    assert [row.id for row in rows] == [item.id]
    assert total == 1


async def test_the_trash_does_not_list_purged_rows(
    session: AsyncSession, alice: User
) -> None:
    """A purged row still carries `deleted_at`, which is the whole reason `purged_at` exists.

    Without it the trash page would offer a restore that returns an empty memory.
    """
    item = await _seed(session, alice)
    service = _service(session)
    await service.delete(item.id, alice.id)
    await service.purge(item.id, alice.id)
    await session.commit()

    rows, total = await service.list_trash(alice.id, limit=20, offset=0)

    assert list(rows) == []
    assert total == 0


async def test_the_trash_is_scoped_to_its_owner(
    session: AsyncSession, alice: User
) -> None:
    item = await _seed(session, alice)
    service = _service(session)
    await service.delete(item.id, alice.id)
    await session.commit()

    rows, total = await service.list_trash(uuid.uuid4(), limit=20, offset=0)

    assert list(rows) == []
    assert total == 0


# --- restore ------------------------------------------------------------------------------


async def test_restore_brings_the_memory_back_whole(
    session: AsyncSession, alice: User
) -> None:
    item = await _seed(session, alice, chunks=2)
    repo = VaultRepository(session)
    service = VaultService(repo, _Storage())  # type: ignore[arg-type]
    await service.delete(item.id, alice.id)
    await session.commit()

    restored = await service.restore(item.id, alice.id)
    await session.commit()

    assert restored is not None
    live = await repo.get(item.id, alice.id)
    assert live is not None
    assert live.title == "Redis persistence"
    assert live.storage_key == "users/a/b/c.pdf"
    rows, total = await repo.list_for_user(alice.id)
    assert [row.id for row in rows] == [item.id]
    assert total == 1
    remaining = await session.exec(
        select(func.count())
        .select_from(VaultChunk)
        .where(col(VaultChunk.vault_item_id) == item.id)
    )
    assert remaining.one() == 2


async def test_restore_refuses_someone_elses_memory(
    session: AsyncSession, alice: User
) -> None:
    """The id is in the URL, so this is where a stranger's would be easiest to slip in."""
    item = await _seed(session, alice)
    service = _service(session)
    await service.delete(item.id, alice.id)
    await session.commit()

    assert await service.restore(item.id, uuid.uuid4()) is None


async def test_restore_refuses_a_live_memory(session: AsyncSession, alice: User) -> None:
    """Nothing to restore, and a success here would be a write that changed nothing."""
    item = await _seed(session, alice)

    assert await _service(session).restore(item.id, alice.id) is None


async def test_restore_refuses_a_purged_memory(session: AsyncSession, alice: User) -> None:
    """There is nothing left in it -- restoring would hand back an empty card."""
    item = await _seed(session, alice)
    service = _service(session)
    await service.delete(item.id, alice.id)
    await service.purge(item.id, alice.id)
    await session.commit()

    assert await service.restore(item.id, alice.id) is None


# --- purge: the irreversible half ----------------------------------------------------------


async def test_purge_scrubs_everything_the_memory_held(
    session: AsyncSession, alice: User
) -> None:
    """The person asked for it to be gone. A tombstone that still holds the text is not gone."""
    item = await _seed(session, alice)
    service = _service(session)
    await service.delete(item.id, alice.id)

    assert await service.purge(item.id, alice.id) is True
    await session.commit()

    raw = await session.get(VaultItem, item.id)
    assert raw is not None
    assert raw.purged_at is not None
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


async def test_purge_takes_the_chunks(session: AsyncSession, alice: User) -> None:
    """They carry the same words *and* the vector drawn from them.

    Leaving them keeps deleted text searchable in the one index built to find it.
    """
    item = await _seed(session, alice, chunks=3)
    service = _service(session)
    await service.delete(item.id, alice.id)
    await service.purge(item.id, alice.id)
    await session.commit()

    remaining = await session.exec(
        select(func.count())
        .select_from(VaultChunk)
        .where(col(VaultChunk.vault_item_id) == item.id)
    )
    assert remaining.one() == 0


async def test_purge_removes_the_stored_object(session: AsyncSession, alice: User) -> None:
    """The key is read before the scrub clears it, or the bytes would be orphaned."""
    item = await _seed(session, alice)
    storage = _Storage()
    service = VaultService(VaultRepository(session), storage)  # type: ignore[arg-type]
    await service.delete(item.id, alice.id)

    await service.purge(item.id, alice.id)

    assert storage.deleted == ["users/a/b/c.pdf"]


async def test_purge_refuses_a_live_memory(session: AsyncSession, alice: User) -> None:
    """"Delete forever" is a second action on something already thrown away.

    A live item answering here would be a route that destroys a memory in one call, which
    is the thing the trash was added to stop.
    """
    item = await _seed(session, alice)

    assert await _service(session).purge(item.id, alice.id) is False
    raw = await session.get(VaultItem, item.id)
    assert raw is not None and raw.title == "Redis persistence"


async def test_purge_refuses_someone_elses_memory(
    session: AsyncSession, alice: User
) -> None:
    item = await _seed(session, alice)
    service = _service(session)
    await service.delete(item.id, alice.id)
    await session.commit()

    assert await service.purge(item.id, uuid.uuid4()) is False


# --- the sweep ------------------------------------------------------------------------------


async def test_the_sweep_leaves_a_memory_inside_its_window(
    session: AsyncSession, alice: User
) -> None:
    """The whole point of the window. A sweep that ignored it would be the old delete."""
    item = await _seed(session, alice)
    service = _service(session)
    await service.delete(item.id, alice.id)
    await _age_out(session, item, days=settings.TRASH_RETENTION_DAYS - 1)

    assert await service.purge_expired() == 0
    await session.commit()

    raw = await session.get(VaultItem, item.id)
    assert raw is not None and raw.purged_at is None and raw.title is not None


async def test_the_sweep_purges_a_memory_past_its_window(
    session: AsyncSession, alice: User
) -> None:
    item = await _seed(session, alice)
    storage = _Storage()
    service = VaultService(VaultRepository(session), storage)  # type: ignore[arg-type]
    await service.delete(item.id, alice.id)
    await _age_out(session, item, days=settings.TRASH_RETENTION_DAYS + 1)

    assert await service.purge_expired() == 1
    await session.commit()

    raw = await session.get(VaultItem, item.id)
    assert raw is not None
    assert raw.purged_at is not None
    assert raw.title is None
    assert storage.deleted == ["users/a/b/c.pdf"]


async def test_the_sweep_never_touches_a_live_memory(
    session: AsyncSession, alice: User
) -> None:
    """It reads `deleted_at IS NOT NULL`. An old memory is not a deleted one."""
    item = await _seed(session, alice)
    raw = await session.get(VaultItem, item.id)
    assert raw is not None
    raw.created_at = datetime.now(UTC) - timedelta(days=365)
    session.add(raw)
    await session.commit()

    assert await _service(session).purge_expired() == 0
    live = await VaultRepository(session).get(item.id, alice.id)
    assert live is not None and live.title == "Redis persistence"


async def test_the_sweep_does_not_purge_twice(session: AsyncSession, alice: User) -> None:
    """`purged_at` is what keeps an already-scrubbed row out of the next tick's batch."""
    item = await _seed(session, alice)
    service = _service(session)
    await service.delete(item.id, alice.id)
    await _age_out(session, item, days=settings.TRASH_RETENTION_DAYS + 1)

    assert await service.purge_expired() == 1
    await session.commit()
    assert await service.purge_expired() == 0


async def test_empty_trash_purges_only_this_users_rows(
    session: AsyncSession, alice: User, bob: User
) -> None:
    """The batch is scoped by owner, like every other write in this file."""
    mine = await _seed(session, alice)
    theirs = await _seed(session, bob)
    service = _service(session)
    await service.delete(mine.id, alice.id)
    await service.delete(theirs.id, bob.id)
    await session.commit()

    assert await service.empty_trash(alice.id) == 1
    await session.commit()

    mine_raw = await session.get(VaultItem, mine.id)
    theirs_raw = await session.get(VaultItem, theirs.id)
    assert mine_raw is not None and mine_raw.purged_at is not None
    assert theirs_raw is not None and theirs_raw.purged_at is None
    assert theirs_raw.title == "Redis persistence"
