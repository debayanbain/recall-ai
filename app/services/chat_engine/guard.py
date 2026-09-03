"""What the agent said, checked against what it was actually shown.

`validation.validate_answer` does the mechanical work and is unchanged; this is the thin
layer that decides *which* evidence and *which* cap a given turn is judged against. Two
decisions live here and nowhere else.

**The allowlist is the toolbox's surfaced set, never a list the model supplied.** A model
that has invented a citation is exactly the model that will also list it as evidence.

**A turn that called no tool is capped harder.** With no tool call there is no retrieved
evidence behind a word of it, so a long reply is not a thorough answer -- it is the model
speaking in its own voice about something else, which is the failure the old closed scope
gate existed to prevent and could not measure. `agent_long_reply_no_tools` is the
replacement, and it is a measurement rather than a gate: the reply is trimmed, the flag is
logged, and reading a week of them says whether the prompt is holding.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import get_logger
from app.services.chat_engine.toolbox import SurfacedSet
from app.services.chat_engine.validation import validate_answer

log = get_logger("recall.agent")

#: The flag raised when the smaller cap actually bit. Named for the log, and the one
#: number worth watching after the scope gate stops blocking.
LONG_REPLY_NO_TOOLS = "agent_long_reply_no_tools"


@dataclass(frozen=True, slots=True)
class GuardResult:
    """The text as it may be shown, and what had to be done to it to get there."""

    text: str
    removed: tuple[str, ...] = ()
    rejected: bool = False
    flag: str | None = None

    @property
    def ids_removed(self) -> int:
        return sum(1 for entry in self.removed if entry.startswith("unknown-id:"))

    @property
    def urls_removed(self) -> int:
        return sum(1 for entry in self.removed if entry.startswith("unknown-url:"))

    @property
    def trimmed(self) -> bool:
        return "length" in self.removed


def guard(
    answer: str | None,
    surfaced: SurfacedSet,
    *,
    used_tools: bool,
    max_chars: int | None = None,
) -> GuardResult:
    """Check one reply against the evidence this turn actually surfaced."""
    cap = max_chars if max_chars is not None else settings.RECALL_ANSWER_MAX_CHARS
    flag: str | None = None
    if not used_tools:
        # Deliberately the conversation lane's cap, not the answer lane's: an answer with
        # no evidence behind it has no honest reason to be long.
        cap = min(cap, settings.CHAT_REPLY_MAX_CHARS)

    checked = validate_answer(
        answer,
        allowed_ids=surfaced.ids,
        allowed_urls=surfaced.urls,
        max_chars=cap,
    )
    if not used_tools and "length" in checked.removed:
        flag = LONG_REPLY_NO_TOOLS
        log.info(LONG_REPLY_NO_TOOLS, chars=len(answer or ""), cap=cap)
    if checked.removed:
        # The clearest fabrication signal the system has: the model named a memory or a
        # link that nothing in front of it carried.
        log.warning("agent_answer_corrected", removed=list(checked.removed[:5]))
    return GuardResult(
        text=checked.text,
        removed=checked.removed,
        rejected=checked.rejected,
        flag=flag,
    )
