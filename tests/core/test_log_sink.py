"""Redaction: what must never reach a log file, and what must not be mangled on the way.

A log file gets copied, pasted into an issue and archived, so a credential that reaches
the disk has already leaked -- which is why `_is_sensitive` matches key names loosely.
The cost of that looseness is the case below: a field whose name merely *contains* a
sensitive word gets redacted even when it is an integer counting usage.
"""
from __future__ import annotations

from app.core.log_sink import build_record


def test_a_credential_never_reaches_the_record() -> None:
    record = build_record(
        {"event": "outbound", "bot_token": "12345:AAH", "api_key": "sk-live"}, "worker"
    )
    context = record["context"]

    assert context["bot_token"] == "[redacted]"
    assert context["api_key"] == "[redacted]"


# --- token counts are numbers, not credentials ---------------------------------------


def test_usage_counts_survive_redaction() -> None:
    """`input_tokens` matches "token" and was redacted, defeating usage logging.

    Found against a live provider call: the field was present and looked deliberate,
    carrying `[redacted]` where the number belonged -- a measurement that reads as
    working and reports nothing.
    """
    record = build_record(
        {"event": "model_call", "input_tokens": 1364, "output_tokens": 42}, "worker"
    )
    context = record["context"]

    assert context["input_tokens"] == 1364
    assert context["output_tokens"] == 42


def test_the_exemption_is_exact_and_does_not_open_a_hole() -> None:
    """A loose allowlist would let a real credential through by naming it well."""
    record = build_record(
        {
            "event": "model_call",
            "input_tokens_secret": "sk-live-123",
            "my_input_tokens": "sk-live-456",
            "bot_token": "12345:AAH",
        },
        "worker",
    )
    context = record["context"]

    assert context["input_tokens_secret"] == "[redacted]"
    assert context["my_input_tokens"] == "[redacted]"
    assert context["bot_token"] == "[redacted]"
