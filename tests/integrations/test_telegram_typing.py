"""The typing indicator: shown while the work runs, and never able to break it.

Telegram drops a chat action after about five seconds and offers no way to cancel one, so
the two properties worth pinning are that it repeats for as long as the block takes and
that it stops the moment the block ends.

The last section pins something duller and more important: that the real paths actually
*call* this. `TelegramClient.send_chat_action` existed for a long time with no caller at
all -- a bug no unit test could have found, because every test of the client called it
directly. So both halves are asserted against the functions the webhook and the worker
really run.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.telegram import typing as typing_module
from app.services.telegram.typing import chat_id_of, typing_action


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.actions: list[tuple[str, str]] = []
        self.fail = fail

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        self.actions.append((chat_id, action))
        if self.fail:
            raise RuntimeError("telegram is unreachable")


# --- which chat -----------------------------------------------------------------------


def test_a_private_chat_is_read_straight_off_the_payload() -> None:
    """Before parsing, and long before the sender is resolved to an account."""
    update = {"message": {"chat": {"id": 555000, "type": "private"}, "text": "hi"}}

    assert chat_id_of(update) == "555000"


def test_a_group_chat_gets_no_indicator() -> None:
    """The bot does not act in a room, so it must not look like it is about to."""
    update = {"message": {"chat": {"id": -100, "type": "group"}, "text": "hi"}}

    assert chat_id_of(update) is None


@pytest.mark.parametrize(
    "update",
    [
        {},
        {"message": None},
        {"message": {}},
        {"message": {"chat": "not-a-dict"}},
        {"message": {"chat": {"type": "private"}}},
    ],
)
def test_a_malformed_update_yields_no_chat_rather_than_raising(update: Any) -> None:
    """This runs before the update has been validated by anything."""
    assert chat_id_of(update) is None


# --- the loop -------------------------------------------------------------------------


async def test_the_action_is_sent_immediately() -> None:
    client = FakeClient()

    async with typing_action(client, "555000"):
        # One scheduling turn is all the background task needs to make its first call.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert client.actions[0] == ("555000", "typing")


async def test_it_repeats_while_the_work_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """One action at the start covers five seconds and then goes quiet mid-wait, which
    reads as the bot having given up."""
    monkeypatch.setattr(typing_module, "REFRESH_SECONDS", 0.01)
    client = FakeClient()

    async with typing_action(client, "555000"):
        await asyncio.sleep(0.05)

    assert len(client.actions) >= 3


async def test_it_stops_when_the_block_ends() -> None:
    monkeypatch_free_client = FakeClient()

    async with typing_action(monkeypatch_free_client, "555000"):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    sent = len(monkeypatch_free_client.actions)
    await asyncio.sleep(0.02)

    assert len(monkeypatch_free_client.actions) == sent


async def test_it_gives_up_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung task leaves a chat that stopped typing, not one typing forever."""
    monkeypatch.setattr(typing_module, "REFRESH_SECONDS", 0.01)
    monkeypatch.setattr(typing_module, "MAX_SECONDS", 0.03)
    client = FakeClient()

    async with typing_action(client, "555000"):
        await asyncio.sleep(0.15)

    assert len(client.actions) <= 4


# --- and never in the way -------------------------------------------------------------


async def test_no_chat_id_is_a_no_op_rather_than_a_branch_at_every_call_site() -> None:
    client = FakeClient()

    async with typing_action(client, None):
        pass

    assert client.actions == []


async def test_a_failing_indicator_does_not_fail_the_work() -> None:
    """A cosmetic dot must never turn a successful capture into a Celery retry."""
    client = FakeClient(fail=True)
    done = False

    async with typing_action(client, "555000"):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        done = True

    assert done


async def test_the_work_is_what_propagates() -> None:
    """The indicator wraps the work; it must not swallow the work's own failure."""
    client = FakeClient()

    with pytest.raises(ValueError):
        async with typing_action(client, "555000"):
            raise ValueError("the capture failed")


# --- the wiring, which is the part that was actually missing --------------------------


class RecordingClient(FakeClient):
    """A client that records the *order* of calls, not just that they happened."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def __aenter__(self) -> RecordingClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        self.calls.append("sendChatAction")
        await super().send_chat_action(chat_id, action)

    async def send_message(self, chat_id: str, text: str, **kwargs: Any) -> None:
        self.calls.append("sendMessage")


async def test_the_worker_types_before_it_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through `_handle_telegram_update`, the function the task really runs.

    Everything below it is stood in for -- there is no database, no broker and no Bot
    API here -- because the claim under test is only that the dispatch is wrapped.
    """
    from contextlib import asynccontextmanager

    from app.queue import tasks
    from app.services.telegram import client as client_module
    from app.services.telegram import dispatch as dispatch_module

    recorder = RecordingClient()

    class FakeSession:
        async def commit(self) -> None:
            return None

    @asynccontextmanager
    async def _session() -> Any:
        yield FakeSession()

    class FakeDispatcher:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def handle(self, update: dict[str, Any]) -> Any:
            # A real dispatch awaits a database round trip; this stands in for the
            # yield point that lets the indicator's task run at all.
            await asyncio.sleep(0)
            return dispatch_module.DispatchResult(reply="done", chat_id="4242")

    monkeypatch.setattr(tasks, "task_session", _session)
    monkeypatch.setattr(client_module, "TelegramClient", lambda *a, **k: recorder)
    monkeypatch.setattr(dispatch_module, "TelegramDispatcher", FakeDispatcher)
    monkeypatch.setattr(tasks, "_recall_responder", lambda repo: None)
    monkeypatch.setattr("app.storage.get_storage", lambda: None)

    await tasks._handle_telegram_update(
        {"update_id": 1, "message": {"chat": {"id": 4242, "type": "private"}, "text": "hi"}}
    )

    assert recorder.calls[0] == "sendChatAction", "the dot has to come before the reply"
    assert "sendMessage" in recorder.calls


async def test_the_worker_does_not_type_into_a_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from app.queue import tasks
    from app.services.telegram import client as client_module
    from app.services.telegram import dispatch as dispatch_module

    recorder = RecordingClient()

    class FakeSession:
        async def commit(self) -> None:
            return None

    @asynccontextmanager
    async def _session() -> Any:
        yield FakeSession()

    class FakeDispatcher:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def handle(self, update: dict[str, Any]) -> Any:
            await asyncio.sleep(0)
            return dispatch_module.DispatchResult()

    monkeypatch.setattr(tasks, "task_session", _session)
    monkeypatch.setattr(client_module, "TelegramClient", lambda *a, **k: recorder)
    monkeypatch.setattr(dispatch_module, "TelegramDispatcher", FakeDispatcher)
    monkeypatch.setattr(tasks, "_recall_responder", lambda repo: None)
    monkeypatch.setattr("app.storage.get_storage", lambda: None)

    await tasks._handle_telegram_update(
        {"update_id": 1, "message": {"chat": {"id": -100, "type": "group"}, "text": "hi"}}
    )

    assert recorder.calls == []


async def test_the_single_action_opens_and_closes_its_own_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API half. The request path has no long-lived Bot API client to borrow."""
    from app.services.telegram import typing as typing_mod

    recorder = RecordingClient()
    monkeypatch.setattr(typing_mod, "TelegramClient", lambda *a, **k: recorder)

    await typing_mod.send_typing_once("4242")

    assert recorder.actions == [("4242", "typing")]


async def test_the_single_action_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """It runs after the 202 has been decided; an exception here is a 500 on a webhook
    Telegram would then redeliver forever."""
    from app.services.telegram import typing as typing_mod

    def _explode(*a: Any, **k: Any) -> Any:
        raise RuntimeError("no token configured")

    monkeypatch.setattr(typing_mod, "TelegramClient", _explode)

    await typing_mod.send_typing_once("4242")  # must not raise


async def test_no_chat_means_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.telegram import typing as typing_mod

    recorder = RecordingClient()
    monkeypatch.setattr(typing_mod, "TelegramClient", lambda *a, **k: recorder)

    await typing_mod.send_typing_once("")

    assert recorder.actions == []
