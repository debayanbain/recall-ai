"""The check *after* the model, on the way out.

A prompt is a request, not a constraint. Every rule in `ai/chat/chain.py` -- speak only
from the blocks, never invent a URL, never print an id -- is a sentence the model usually
follows, and "usually" is the whole problem: the answer that breaks the rule is exactly
the answer nobody notices, because it reads like the others. So the properties that can
be checked mechanically are checked mechanically, here, against the evidence that was
actually supplied.

Deterministic, and deliberately so. A second model asked "is this answer supported?"
doubles the cost, doubles the latency and puts the judgement back inside the thing being
judged. What is checkable without one:

* **A citation must name a memory that was supplied.** `[a3f1c920]` is the handle the
  prompt labels blocks with, so an id in the answer that was not in the context is the
  model having produced a memory reference from nothing. Removed, and logged -- it is
  the clearest fabrication signal the system has.
* **A URL must have come from a memory.** Nothing else in the answer can be checked
  against a source, but a link can: the only URLs the model was shown are in the fence
  headers. An invented one is both a false claim about the vault and a link a person is
  invited to tap, in text derived from scraped pages.
* **An empty answer is not an answer.** A provider that returns whitespace must reach
  the caller as a failure, not as a message the user reads as the bot ignoring them.
* **Length is bounded.** A grounded answer about a handful of cards has no honest reason
  to run long, and an answer that runs away from its evidence is the shape a fabrication
  takes.

What this cannot do is verify a *claim*: "you saved this on 12 August" is checkable only
against structured evidence, and doing it properly is claim extraction plus per-claim
verification -- a real design, not a regex, and deliberately not built here. This is the
floor, not the ceiling.

Pure and offline. `tests/chat_engine/test_validation.py` pins each rule.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: The id form the prompt uses: eight hex characters in square brackets. Matched exactly
#: that narrowly so ordinary bracketed prose in an answer is left alone.
_ID_RE = re.compile(r"\[([0-9a-f]{8})\]")

#: Trailing punctuation a model puts *after* a URL rather than in it. Stripped before
#: comparison, so "…example.com/a." matches the stored "…example.com/a".
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_URL_TAIL = ".,;:!?)]}'\""

#: What a URL is replaced with rather than deleted outright. A sentence that silently
#: loses its object reads as a bug; this reads as the answer declining to link.
_LINK_REMOVED = "[link omitted]"


@dataclass(frozen=True, slots=True)
class ValidatedAnswer:
    """The text to actually send, and what had to be taken out of it to get there."""

    text: str
    #: Empty unless something was stripped. Every entry is a fabrication the model
    #: produced -- worth a log line, and the input to any later tightening.
    removed: tuple[str, ...] = ()
    #: True when nothing usable survived. The caller must not send this.
    rejected: bool = False

    @property
    def clean(self) -> bool:
        return not self.removed and not self.rejected


def validate_answer(
    text: str | None,
    *,
    allowed_ids: Sequence[str] = (),
    allowed_urls: Iterable[str | None] = (),
    max_chars: int = 1500,
) -> ValidatedAnswer:
    """Check one generated answer against the evidence it was generated from."""
    answer = (text or "").strip()
    if not answer:
        return ValidatedAnswer("", ("empty",), rejected=True)

    removed: list[str] = []
    known_ids = {value.lower() for value in allowed_ids}
    known_urls = {_canonical(url) for url in allowed_urls if url}

    def _drop_unknown_id(match: re.Match[str]) -> str:
        if match.group(1).lower() in known_ids:
            # A real id, but the reply is read by a person and "[a3f1c920]" means
            # nothing to them. The prompt asks for titles; this enforces it.
            removed.append(f"id:{match.group(1)}")
            return ""
        removed.append(f"unknown-id:{match.group(1)}")
        return ""

    answer = _ID_RE.sub(_drop_unknown_id, answer)

    def _drop_unknown_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        trimmed = raw.rstrip(_URL_TAIL)
        tail = raw[len(trimmed):]
        if _canonical(trimmed) in known_urls:
            return raw
        removed.append(f"unknown-url:{trimmed[:120]}")
        return _LINK_REMOVED + tail

    answer = _URL_RE.sub(_drop_unknown_url, answer)

    # Collapse the runs of spaces a removal leaves behind, without touching line breaks:
    # the reply is rendered in a chat window and its paragraphs are load-bearing.
    answer = re.sub(r"[ \t]{2,}", " ", answer)
    answer = "\n".join(line.strip() for line in answer.splitlines()).strip()

    if not answer:
        return ValidatedAnswer("", tuple(removed) or ("empty",), rejected=True)

    if len(answer) > max_chars:
        answer = _clip(answer, max_chars)
        removed.append("length")

    return ValidatedAnswer(answer, tuple(removed))


def _clip(value: str, limit: int) -> str:
    """Trim at a sentence boundary where there is one, a word boundary otherwise."""
    cut = value[:limit]
    stop = max(cut.rfind(". "), cut.rfind("\n"))
    if stop > limit // 2:
        return cut[: stop + 1].rstrip()
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _canonical(url: str) -> str:
    """Enough normalisation that a trailing slash is not a fabrication."""
    return url.strip().rstrip("/").lower()
