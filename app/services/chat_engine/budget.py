"""What one turn is allowed to spend, and what happens when it has spent it.

An agent loop with no ceiling is an unbounded bill reachable through a text box, so the
harness carries four of them: tool calls, model rounds, wall clock, and how many memory
cards may be poured into the context window. The interesting design is not the numbers --
they are settings -- but the shape of running out.

**Exhausting the budget must not break the conversation.** A tool call left without its
`ToolMessage` is a malformed exchange and providers reject the *next* request outright,
so past the ceiling the tools still run and still answer; they simply answer
`SPENT` instead of doing the work. The model then gets one more round with no tools bound
and an instruction to answer with what it has, and the turn ends with words. A turn that
ends with a half-finished plan and no sentence is the one outcome worth engineering
against: the person asked something and got nothing.

Rounds are counted by the graph (they are its steps); calls and cards are counted here,
because the toolbox is the only place that can see them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

#: Handed back in place of a tool result once the call budget is gone. It has to say
#: which of the two states this is: a model that cannot tell "you may not search again"
#: from "the vault is empty" answers the second when the first is true.
SPENT = (
    "Your search budget for this question is spent. Do not call any more tools. Answer "
    "now from what is already in front of you, and if it does not answer the question, "
    "say so plainly."
)

#: Appended to a truncated listing. The number is the point -- "and 9 more" is a fact the
#: person can act on, while a silently short list is one they cannot even notice.
MORE_FMT = "\n\n…and {count} more. Ask me to narrow it down."


@dataclass(slots=True)
class Budget:
    """One turn's allowance. Constructed per turn and thrown away with it."""

    max_calls: int
    max_rounds: int
    wall_clock_seconds: float
    max_cards: int
    calls_used: int = 0
    cards_used: int = 0
    #: Monotonic, not wall time: the loop measures its own duration, and a clock that can
    #: step backwards over an NTP correction would hand it a negative elapsed time.
    started_at: float = field(default_factory=time.monotonic)

    def spend_call(self) -> bool:
        """Take one tool call. False means the caller must answer `SPENT` instead.

        The counter moves either way. A refused call still cost a model round to produce,
        and not counting it lets a model that keeps calling past the ceiling do so
        forever at one refusal per round.
        """
        self.calls_used += 1
        return self.calls_used <= self.max_calls and not self.out_of_time()

    def take_cards(self, available: int) -> int:
        """How many of `available` cards this turn may still render."""
        room = max(0, self.max_cards - self.cards_used)
        taken = min(room, available)
        self.cards_used += taken
        return taken

    def out_of_time(self) -> bool:
        return self.elapsed >= self.wall_clock_seconds

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def exhausted(self) -> bool:
        """True once no further tool call can do any work."""
        return self.calls_used >= self.max_calls or self.out_of_time()


def from_settings() -> Budget:
    """The configured allowance. A function, not a constant: `settings` is an lru_cached
    singleton read at import, and a module-level Budget would freeze `started_at` at
    import time -- every turn in the process would then start already out of time."""
    from app.core.config import settings

    return Budget(
        max_calls=settings.AGENT_MAX_TOOL_CALLS,
        max_rounds=settings.AGENT_MAX_ROUNDS,
        wall_clock_seconds=float(settings.AGENT_WALL_CLOCK_SECONDS),
        max_cards=settings.AGENT_MAX_CONTEXT_CARDS,
    )
