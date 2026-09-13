"""That something actually calls the derivation, from *both* completion paths.

This file exists because of a lesson already written into `CLAUDE.md` about
`sendChatAction`: "the method exists and is tested" is precisely what was true while
nothing called it. A deriver with green unit tests and no caller is a feature that
silently does not happen, and the way it presents is an empty Connections page that looks
like a threshold problem.

Both paths, because they are genuinely separate. `_process_item` finishes a fast capture
(an article, a YouTube link); `_finalize_run` finishes a deferred one once Apify calls
back. Wiring only the first is how the Instagram and Facebook half of a vault quietly has
no connections at all.

Offline: the enqueue is stubbed, so this asserts the call, not Redis.
"""
from __future__ import annotations

import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.base import ProcessingStatus
from app.models.user import User
from tests.conftest import make_item

# Imported inside each test, never at module scope, and this is not style.
# `app/queue/tasks.py` calls `configure_logging(source="worker")` at import -- a
# deliberate side effect, because a prefork worker child has no other hook to run it in --
# and that turns on structlog's `cache_logger_on_first_use` for the whole process. Any
# module importing it during *collection* therefore rewires logging before a single test
# runs, which silently breaks `tests/ai/test_usage_logging.py`'s capture fixture. No other
# test module imports it eagerly either; this comment is why.


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def __call__(self, item_id: uuid.UUID) -> None:
        self.calls.append(item_id)


async def test_a_completed_capture_queues_its_connection_scan(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.queue import tasks

    item = await make_item(session, alice, "finished")
    recorder = _Recorder()
    monkeypatch.setattr(tasks, "enqueue_derive_connections", recorder)

    await tasks._derive_connections(item.id, session)

    assert recorder.calls == [item.id]


async def test_an_unfinished_capture_queues_nothing(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`skipped` and `failed` have no embedding to compare, so the scan would read a row,
    find nothing and write nothing -- a queue hop and a database round trip to reach the
    answer the status already gave."""
    from app.queue import tasks

    item = await make_item(session, alice, "skipped")
    item.processing_status = ProcessingStatus.skipped
    session.add(item)
    await session.commit()
    recorder = _Recorder()
    monkeypatch.setattr(tasks, "enqueue_derive_connections", recorder)

    await tasks._derive_connections(item.id, session)

    assert recorder.calls == []


async def test_an_unreachable_broker_does_not_break_the_capture(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-soft, like `_notify_surface` beside it: by the time this runs the memory is
    enriched, committed and already answered for. An unreachable Redis must cost the user
    their connections, never their capture."""
    from app.queue import tasks

    item = await make_item(session, alice, "finished")

    async def _explode(_: uuid.UUID) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(tasks, "enqueue_derive_connections", _explode)

    await tasks._derive_connections(item.id, session)  # must not raise


def test_both_completion_paths_call_it() -> None:
    """Read the source of the two finishers and require the call in each.

    Blunt on purpose. The alternative is driving the whole pipeline twice with Apify
    stubbed, which is a lot of machinery to assert one line -- and the failure this
    guards against is exactly "somebody adds a third completion path and wires the
    surface notification but not this".
    """
    import inspect

    from app.queue import tasks

    for finisher in (tasks._process_item, tasks._finalize_run):
        source = inspect.getsource(finisher)
        assert "_derive_connections(" in source, finisher.__name__
        # And beside the surface notification, which is the marker for "after the commit".
        assert "_notify_surface(" in source, finisher.__name__


def test_the_task_retries_at_most_once() -> None:
    """Three retries is right for a timeout and wrong here. A `UniqueViolation` from two
    captures racing is absorbed by `ON CONFLICT`, and an item with no embedding is an
    *answer* -- the `VisionError`/`VisionFailed` split. One extra attempt covers a dropped
    database connection; three spend real work to reach the same place."""
    from app.queue import tasks

    assert tasks.derive_connections.max_retries == 1


def test_the_task_is_registered() -> None:
    from app.queue import tasks  # noqa: F401 - registers the task
    from app.queue.celery_app import celery_app

    assert "app.queue.tasks.derive_connections" in celery_app.tasks

