"""Turning an exception into text that is safe to store and show its owner.

`VaultItem.processing_error` is written by the worker and read by the person whose item
failed, so it has to survive two trips it was never designed for: into a database column,
and onto a page. The provider messages that land here are not curated -- an httpx error
carries the full request URL, and some of those URLs carry credentials in the query
string (`api.apify.com/v2/...?token=apify_api_...`). Storing that puts a live token in a
row that is later rendered, copied into a support ticket and pasted into an issue.

So the text is scrubbed on the way *in*, not on the way out. A redaction that only
happens at render time is one that a second render path forgets.

The rule is the same one `log_sink.redact` follows for structured events, applied to free
text instead of keys: anything shaped like a secret goes, and the sentence around it
stays, because "connection refused" and "401 Unauthorized" are the parts a person can act
on.
"""
from __future__ import annotations

import re

REDACTED = "<redacted>"

#: Query parameters whose *value* is a credential. Matched case-insensitively and
#: without assuming the URL parses -- these strings turn up in prose too
#: ("...failed with token=abc").
_SENSITIVE_PARAMS = (
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "key",
    "secret",
    "client_secret",
    "password",
    "signature",
    "sig",
    "x-amz-signature",
    "x-amz-credential",
    "auth",
)

_PARAM_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _SENSITIVE_PARAMS) + r")=[^&\s\"'>)]+",
    re.IGNORECASE,
)

#: Credential *shapes*, for the ones that travel outside a query string: a bearer header
#: echoed into a message, or a provider key quoted verbatim.
_SHAPE_RES = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9._\-]{8,}"),          # OpenAI
    re.compile(r"\bapify_api_[A-Za-z0-9]{8,}"),        # Apify
    re.compile(r"\bAIza[A-Za-z0-9_\-]{10,}"),          # Google
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}"),     # Slack
    re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{30,}"),       # Telegram bot token
)

#: Long enough to say what happened, short enough for a column and a card.
MAX_ERROR_CHARS = 500


def redact_text(text: str) -> str:
    """Strip anything shaped like a credential, keeping the sentence around it."""
    cleaned = _PARAM_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    for pattern in _SHAPE_RES:
        cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned


def safe_error_text(exc: BaseException) -> str:
    """What to store in `processing_error`: the type, the scrubbed message, capped.

    The class name is kept because the message is often empty -- a bare `TimeoutError`
    reads as nothing at all otherwise, and "which provider gave up" is the first thing
    anyone looking at a failed item wants.
    """
    message = redact_text(str(exc)).strip()
    label = type(exc).__name__
    combined = f"{label}: {message}" if message else label
    return combined[:MAX_ERROR_CHARS]
