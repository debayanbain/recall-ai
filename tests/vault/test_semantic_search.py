"""Vector search over `vault_chunks`.

These embeddings and the HNSW index were written on every save for months before anything
read them, so this is the first code that can be wrong about them. Two properties matter
more than ranking quality:

* **Tenant isolation.** The index spans every user's chunks. A missing predicate here
  does not error -- it quietly returns someone else's memories, ranked helpfully.
* **Soft-deleted items stay gone.** Deleting an item does not delete its chunk, so the
  vector outlives the row's visibility and the join is the only thing hiding it.

Needs a real PostgreSQL with pgvector; skipped otherwise, like every DB-backed test here.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.base import ContentType, ProcessingStatus
from app.models.user import User
from app.models.vault import VaultChunk, VaultItem
from app.repositories.vault import VaultRepository

_DIM = settings.EMBEDDING_DIM


def _vector(lead: float) -> list[float]:
    """A vector whose first component decides its distance from `_vector(1.0)`."""
    values = [0.0] * _DIM
    values[0] = lead
    values[1] = 1.0 - lead
    return values


async def _seed(
    session: AsyncSession,
    owner: User,
    title: str,
    lead: float,
    *,
    content_type: ContentType = ContentType.article,
    created_at: datetime | None = None,
    deleted: bool = False,
) -> VaultItem:
    item = VaultItem(
        user_id=owner.id,
        type=content_type,
        title=title,
        summary=f"summary of {title}",
        processing_status=ProcessingStatus.completed,
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    if created_at is not None:
        item.created_at = created_at
    session.add(item)
    await session.flush()
    session.add(
        VaultChunk(
            vault_item_id=item.id,
            user_id=owner.id,
            chunk_index=0,
            content=f"body of {title}",
            embedding=_vector(lead),
        )
    )
    await session.commit()
    await session.refresh(item)
    return item


async def test_results_are_ordered_by_distance(session: AsyncSession, alice: User) -> None:
    far = await _seed(session, alice, "Far", 0.0)
    near = await _seed(session, alice, "Near", 1.0)
    middle = await _seed(session, alice, "Middle", 0.6)

    rows = await VaultRepository(session).search_semantic(alice.id, _vector(1.0), limit=5)

    assert [item.id for item, _ in rows] == [near.id, middle.id, far.id]
    distances = [distance for _, distance in rows]
    assert distances == sorted(distances)


async def test_another_users_memories_are_never_returned(
    session: AsyncSession, alice: User, bob: User
) -> None:
    """The nearest neighbour in the whole index belongs to Bob. Alice must not see it."""
    await _seed(session, bob, "Bob's exact match", 1.0)
    mine = await _seed(session, alice, "Alice's loose match", 0.2)

    rows = await VaultRepository(session).search_semantic(alice.id, _vector(1.0), limit=5)

    assert [item.id for item, _ in rows] == [mine.id]


async def test_soft_deleted_items_are_hidden(session: AsyncSession, alice: User) -> None:
    await _seed(session, alice, "Deleted", 1.0, deleted=True)
    kept = await _seed(session, alice, "Kept", 0.3)

    rows = await VaultRepository(session).search_semantic(alice.id, _vector(1.0), limit=5)

    assert [item.id for item, _ in rows] == [kept.id]


async def test_date_filter_excludes_older_items(session: AsyncSession, alice: User) -> None:
    now = datetime.now(UTC)
    await _seed(session, alice, "Old", 1.0, created_at=now - timedelta(days=30))
    recent = await _seed(session, alice, "Recent", 0.4, created_at=now - timedelta(days=1))

    rows = await VaultRepository(session).search_semantic(
        alice.id, _vector(1.0), limit=5, created_after=now - timedelta(days=5)
    )

    assert [item.id for item, _ in rows] == [recent.id]


async def test_content_type_filter(session: AsyncSession, alice: User) -> None:
    await _seed(session, alice, "Article", 1.0, content_type=ContentType.article)
    video = await _seed(session, alice, "Reel", 0.4, content_type=ContentType.instagram)

    rows = await VaultRepository(session).search_semantic(
        alice.id, _vector(1.0), limit=5, content_types=[ContentType.instagram]
    )

    assert [item.id for item, _ in rows] == [video.id]


async def test_an_item_appears_once_however_many_chunks_match(
    session: AsyncSession, alice: User
) -> None:
    item = await _seed(session, alice, "Long article", 1.0)
    session.add(
        VaultChunk(
            vault_item_id=item.id,
            user_id=alice.id,
            chunk_index=1,
            content="second chunk",
            embedding=_vector(0.95),
        )
    )
    await session.commit()

    rows = await VaultRepository(session).search_semantic(alice.id, _vector(1.0), limit=5)

    assert [i.id for i, _ in rows].count(item.id) == 1


async def test_empty_vault_returns_nothing(session: AsyncSession, alice: User) -> None:
    assert await VaultRepository(session).search_semantic(alice.id, _vector(1.0)) == []


async def test_list_filtered_scopes_and_filters(
    session: AsyncSession, alice: User, bob: User
) -> None:
    now = datetime.now(UTC)
    await _seed(session, bob, "Bob's", 1.0)
    await _seed(session, alice, "Old", 1.0, created_at=now - timedelta(days=30))
    recent = await _seed(session, alice, "Recent", 1.0, created_at=now - timedelta(hours=2))

    items, total = await VaultRepository(session).list_filtered(
        alice.id, limit=10, created_after=now - timedelta(days=5)
    )

    assert [i.id for i in items] == [recent.id]
    assert total == 1


async def test_unknown_user_sees_an_empty_vault(session: AsyncSession, alice: User) -> None:
    await _seed(session, alice, "Alice's", 1.0)
    items, total = await VaultRepository(session).list_filtered(uuid.uuid4(), limit=10)
    assert list(items) == [] and total == 0
