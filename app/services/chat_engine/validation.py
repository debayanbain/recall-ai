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
    answer = scrub(answer, allowed_ids, allowed_urls, removed)

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


def scrub(
    text: str,
    allowed_ids: Sequence[str],
    allowed_urls: Iterable[str | None],
    removed: list[str],
) -> str:
    """Strip citations and links the evidence does not support. Appends to `removed`.

    Split out of `validate_answer` because the streaming validator below has to apply
    exactly these rules to a fragment. Two implementations of "which links are allowed"
    is one implementation that stops matching the other, and the one that drifts is
    whichever is used less -- which would be the streaming one, on the surface where the
    text is already on somebody's screen.
    """
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

    def _drop_unknown_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        trimmed = raw.rstrip(_URL_TAIL)
        tail = raw[len(trimmed):]
        if _canonical(trimmed) in known_urls:
            return raw
        removed.append(f"unknown-url:{trimmed[:120]}")
        return _LINK_REMOVED + tail

    return _URL_RE.sub(_drop_unknown_url, _ID_RE.sub(_drop_unknown_id, text))


#: How much unbroken text may accumulate before it is released without a boundary. A
#: model that emits a very long run with no whitespace would otherwise be buffered to the
#: end of the answer, which is a stall the reader reads as a hang.
_MAX_HELD = 400


class StreamValidator:
    """`validate_answer`'s rules, applied to a token stream before anything is displayed.

    Streaming and validation pull against each other: the check that a URL came from a
    memory is a check on finished text, and a token stream is by definition unfinished.
    Sending raw deltas and correcting afterwards is not an option -- the whole point of
    the URL rule is that a fabricated link is one a person is invited to *tap*, and it
    has already been tapped by the time a correction arrives.

    What makes it work is that both checkable things -- `[a3f1c920]` and a URL -- contain
    no whitespace. So the validator emits only up to the last whitespace it has seen and
    holds the trailing run back: whatever is released has been seen whole, and is put
    through the same `scrub` the non-streaming path uses.

    The length cap is enforced as it goes, and `finish()` closes the stream: it releases
    the last held run and reports whether anything was stripped, which the caller sends
    as its terminal event.
    """

    def __init__(
        self,
        *,
        allowed_ids: Sequence[str] = (),
        allowed_urls: Iterable[str | None] = (),
        max_chars: int = 1500,
    ) -> None:
        self.allowed_ids = list(allowed_ids)
        self.allowed_urls = list(allowed_urls)
        self.max_chars = max_chars
        self.removed: list[str] = []
        self.emitted = 0
        self.removed_len = 0
        self._held = ""
        self._stopped = False
        #: Whether anything but whitespace has been released. `emitted` counts characters
        #: and a run of spaces is characters; a reply made only of them is the same
        #: nothing the finished path rejects.
        self._produced = False
        #: Whether the last release ended on whitespace, so a fragment starting with more
        #: of it can be collapsed. The finished path squeezes runs of spaces after a
        #: removal; without this the streaming path would leave the gap a stripped id
        #: left behind, and the two would disagree on the one input that matters.
        self._ended_blank = True

    def feed(self, chunk: str) -> str:
        """Take one delta, return what is safe to display now -- often nothing."""
        if self._stopped or not chunk:
            return ""
        self._held += chunk
        boundary = max(self._held.rfind(" "), self._held.rfind("\n"), self._held.rfind("\t"))
        if boundary < 0:
            if len(self._held) < _MAX_HELD:
                return ""
            boundary = len(self._held) - 1
        ready, self._held = self._held[: boundary + 1], self._held[boundary + 1 :]
        return self._release(ready)

    def finish(self) -> str:
        """Release whatever is still held. Call exactly once, at the end of the stream."""
        if self._stopped:
            return ""
        tail, self._held = self._held, ""
        out = self._release(tail)
        self._stopped = True
        return out

    @property
    def rejected(self) -> bool:
        """True when nothing usable was ever produced -- a failure, not a blank message."""
        return not self._produced

    def _release(self, text: str) -> str:
        if not text:
            return ""
        cleaned = scrub(text, self.allowed_ids, self.allowed_urls, self.removed)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        if self._ended_blank:
            cleaned = cleaned.lstrip(" \t")
        if not cleaned:
            return ""

        room = self.max_chars - self.emitted
        if room <= 0:
            self._stop_for_length()
            return ""
        if len(cleaned) > room:
            # Clipped mid-stream rather than at the end: the cap exists so a reply that
            # has run away from its evidence stops, and stopping late is not stopping.
            cleaned = _clip(cleaned, room)
            self._stop_for_length()

        self.emitted += len(cleaned)
        self._produced = self._produced or bool(cleaned.strip())
        self._ended_blank = cleaned[-1] in " \t\n"
        return cleaned

    def _stop_for_length(self) -> None:
        if "length" not in self.removed:
            self.removed.append("length")
        self._stopped = True
