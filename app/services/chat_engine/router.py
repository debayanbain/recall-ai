"""What a message *is*, decided by its shape and nothing else.

One pure function, no I/O, no model call. It runs on every inbound message, so it must
cost nothing; and it is the thing that decides whether text is kept, so it must be
readable in one screen and testable without a database, a queue or a surface.

The classification is deliberately structural rather than semantic. Asking a model "did
they mean to save this?" makes capture non-deterministic and unexplainable: the same
sentence would sometimes become a memory and sometimes not, and the user has no way to
learn the rule. Shape is a rule a person can learn in one sentence -- a file or a link is
a save, a slash is a command, everything else is talk unless it is plainly asking about
what they already kept.

Order is the whole design, and it is checked top to bottom:

1. **An attachment is a capture**, before anything else. A photo with no caption still
   has to be saved, and a caption saying "lol" must not turn it into small talk.
2. **Nothing to read is chat.** With no text left there is no shape to route on, and
   inventing an intent from an empty string is how a stray keystroke becomes a memory.
3. **A leading slash is a command**, so a command is never re-read as prose. `/find`
   must reach the command handler and not the recall branch.
4. **A link is a capture**, and it beats the phrasing around it. "what is this?" wrapped
   around a URL is someone pasting a link, not asking a question about their vault --
   answering it instead would drop the link on the floor.
5. **Questions about the assistant** are answered by the assistant, not searched for.
   "what can you do" retrieves nothing, and searching for it spends an embedding to
   return an empty result the user reads as a broken feature.
6. **Retrieval phrasing is recall**, and everything else is chat.

`RECALL` is matched on phrases, never on a trailing `?`. A question mark is the single
most common character in ordinary conversation with an assistant -- "what is the capital
of France?" is a question and has nothing to do with anything the person saved. Routing
on it sends every greeting-shaped question through retrieval, which costs an embedding
and a vector scan to answer "I could not find anything about that."

The caller supplies `url` and `has_attachment`: finding a link inside a provider's
payload is that provider's business, and this module stays free of every surface it
serves so a second one can reuse it unchanged. `tests/chat_engine/test_boundaries.py`
pins that.
"""
from __future__ import annotations

import re
from enum import StrEnum


class Intent(StrEnum):
    """What the caller should do with a message. A `StrEnum`, like every other
    enum in this codebase, so it logs and compares as plain text."""

    COMMAND = "command"
    CAPTURE = "capture"
    META = "meta"
    RECALL = "recall"
    CHAT = "chat"


#: Messages are bounded before matching. Every pattern here is linear on its own, but a
#: caller is free to hand this an arbitrarily long body, and intent is decided by the
#: opening of a message in every case that matters -- so the tail is not worth scanning.
_MAX_SCAN = 2000

# Questions about the assistant itself. Answered, never retrieved.
_META_PATTERNS = (
    r"\bwho are you\b",
    r"\byour name\b",
    r"\bwhat can you do\b",
    r"\bwho made you\b",
    r"\bare you (?:a |an )?(?:bot|human|ai)\b",
)

# Retrieval phrasing: the person is pointing at something they already kept.
_RECALL_PATTERNS = (
    r"\bi saved\b",
    r"\bdid i\b",
    r"\bhave i\b",
    r"\bshow me\b",
    r"\bfind\b",
    r"\bremember\b",
    r"\bmy (?:saves|vault|notes|links)\b",
    r"\blast week\b",
    r"\bthis week\b",
    r"\byesterday\b",
)

_META_RE = re.compile("|".join(_META_PATTERNS))
_RECALL_RE = re.compile("|".join(_RECALL_PATTERNS))

# "any cooking videos?" -- a kind word somewhere after "any". Split into two anchored
# scans rather than one `\bany\b.*\bvideos\b`: the wildcard form backtracks across the
# whole message on every near-miss, and this is fed unbounded user text.
_ANY_RE = re.compile(r"\bany\b")
_KIND_RE = re.compile(r"\b(?:videos?|posts?|articles?|reels?)\b")


def _asks_for_a_kind(text: str) -> bool:
    """True for "any <something> videos" and friends, in that order."""
    opener = _ANY_RE.search(text)
    return opener is not None and _KIND_RE.search(text, opener.end()) is not None


def route(
    text: str | None,
    *,
    url: str | None = None,
    has_attachment: bool = False,
) -> Intent:
    """Classify one message. Pure: same inputs, same answer, no side effects."""
    if has_attachment:
        return Intent.CAPTURE

    stripped = (text or "").strip()
    if not stripped:
        return Intent.CHAT

    if stripped.startswith("/"):
        return Intent.COMMAND

    if url is not None:
        return Intent.CAPTURE

    lowered = stripped[:_MAX_SCAN].lower()

    if _META_RE.search(lowered):
        return Intent.META

    if _RECALL_RE.search(lowered) or _asks_for_a_kind(lowered):
        return Intent.RECALL

    return Intent.CHAT
