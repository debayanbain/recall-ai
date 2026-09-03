"""The SSE framing on `POST /chat/ask`.

The route itself needs a database and is covered by the DB-backed suite; what is offline
and worth pinning here is the wire format, because every one of these is a bug the client
sees as a silent hang rather than as an error.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from app.api.v1.chat import AskRequest, _frame
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.types import Delta, ErrorKind, ItemsEvent, StreamEnd

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _parse(frame: str) -> tuple[str, dict[str, object]]:
    name, data = frame.rstrip("\n").split("\n", 1)
    return name.removeprefix("event: "), json.loads(data.removeprefix("data: "))


def test_a_newline_in_an_answer_does_not_break_the_frame() -> None:
    """`data:` may not contain a raw newline, and answer text is full of them. Getting
    this wrong truncates the message at the first paragraph, silently."""
    name, payload = _parse(_frame(Delta(text="line one\nline two")))
    assert name == "delta"
    assert payload["text"] == "line one\nline two"


def test_the_end_event_carries_its_evidence() -> None:
    name, payload = _parse(
        _frame(StreamEnd(memory_ids=("aa11bb22",), corrected=True))
    )
    assert name == "end"
    assert payload == {"memory_ids": ["aa11bb22"], "corrected": True, "error": None}


def test_an_error_is_named_on_the_end_event() -> None:
    _name, payload = _parse(_frame(StreamEnd(error=ErrorKind.chat_unavailable)))
    assert payload["error"] == "chat_unavailable"


def test_a_listing_ships_cards_and_not_bodies() -> None:
    """`VaultItemRead` is the same shape every other listing returns, so nothing reaches
    the browser here that a listing would not already show."""
    item = VaultItem(
        id=uuid.uuid4(),
        user_id=_USER,
        type=ContentType.article,
        title="Pasta",
        content="a very long body nobody asked for",
        processing_status=ProcessingStatus.completed,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )
    _name, payload = _parse(_frame(ItemsEvent(items=[item], total=1)))
    body = json.dumps(payload)
    assert "Pasta" in body
    assert "nobody asked for" not in body
    assert "storage_key" not in body


def test_the_request_body_has_nothing_to_tamper_with() -> None:
    """Everything deciding which rows are read comes from the session. A body carrying a
    user id, a memory id or a prompt override is a body worth attacking."""
    fields = set(AskRequest.model_fields)
    assert fields == {"question", "conversation_id"}


def test_the_question_is_bounded() -> None:
    """It reaches a prompt and a log line."""
    import pydantic

    try:
        AskRequest(question="x" * 5000)
    except pydantic.ValidationError:
        return
    raise AssertionError("an unbounded question was accepted")


# --- the two events a tap needs -------------------------------------------------------


def test_a_question_frames_its_options_with_their_tokens() -> None:
    """The client renders chips; a tapped chip posts its own label as the next question.

    Both halves travel: the label because it is what a person reads, the token because
    it is what identifies the choice when it comes back.
    """
    from app.services.chat_engine.types import Choice, QuestionEvent

    name, payload = _parse(
        _frame(
            QuestionEvent(
                question="Which one did you mean?",
                choices=(
                    Choice(label="the docker talk", token="t1"),
                    Choice(label="the reel", token="t2"),
                ),
            )
        )
    )

    assert name == "question"
    assert payload["question"] == "Which one did you mean?"
    assert payload["options"] == [
        {"label": "the docker talk", "token": "t1"},
        {"label": "the reel", "token": "t2"},
    ]


def test_a_proposal_frames_the_exact_text_that_would_be_written() -> None:
    """Never a summary of it: this is where a person reads what a caption talked the
    model into proposing, and declines."""
    from app.services.chat_engine.types import ProposalEvent

    name, payload = _parse(
        _frame(
            ProposalEvent(
                preview="call the landlord about the leak",
                accept_token="tok",
                action="note",
            )
        )
    )

    assert name == "proposal"
    assert payload == {
        "token": "tok",
        "action": "note",
        "preview": "call the landlord about the leak",
    }


def test_the_confirm_routes_do_not_name_their_parameter_token() -> None:
    """`get_current_user` already takes a `token`, from a cookie, with a default.

    FastAPI resolves path-parameter names across the whole dependency tree, so a path
    parameter called `token` collides with it -- and a path parameter may not have a
    default. Same trap as `/spaces/invites/{invite_token}/accept`, and the reason it is
    pinned here is that the failure is at import time on a route nobody has hit yet.
    """
    import inspect

    from app.api.v1.chat import accept_proposal, decline_proposal

    for route in (accept_proposal, decline_proposal):
        names = set(inspect.signature(route).parameters)
        assert "proposal_token" in names
        assert "token" not in names
