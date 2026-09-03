"""One line per turn, so a regression can be dated and an attack can be seen.

Everything an agent turn did that is worth knowing afterwards, emitted once as
`agent_turn`. It is deliberately one record rather than a line per step: a turn is the
unit a person experienced, and reconstructing it from six scattered events is what makes
a log unreadable at the moment someone needs it.

Two rules about the contents:

* **Tool names, never tool arguments.** The arguments are the model's own guess at the
  subject and are derived from the person's message; a log that carries them carries the
  message. Names alone answer the question the log exists for -- what did it do.
* **The two booleans are self-reports.** `declined_out_of_scope` and `asked_question`
  come from the model's own `FinalAnswer`. They are recorded and never branched on: a
  model talked into answering a general-knowledge question is also a model that will
  report it declined. A rise in either is a prompt to read the messages, not a control.

`surface`, `shadow` and `router_lane` are **not** fields here. They are bound as
structlog contextvars by whoever knows them -- `ChatEngine` already stamps `surface` and
`intent` on every turn, and the shadow task stamps its own two -- so they ride on this
record and on every `model_call` beside it without four signatures growing an argument to
carry them to one log line. That is the pattern this codebase already uses; the comment in
`chat_engine/engine.py` explains why.

Token counts are not here on purpose. `UsageLogger` already emits `model_call` per
provider call with its own input and output counts, and every line of a turn -- these
included -- carries the same `request_id`, so the sum is a `jq` away and does not need a
second, drifting copy.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.ai.prompts import PROMPT_VERSION
from app.core.logging import get_logger

log = get_logger("recall.agent")


@dataclass(slots=True)
class AgentTrace:
    """Accumulated during a turn, emitted once at the end of it."""

    #: Which zero-token lane answered instead, if one did. `None` means the agent ran.
    fast_path: str | None = None
    rounds: int = 0
    tool_calls: list[str] = field(default_factory=list)
    cards_in_context: int = 0
    #: Which rung of the degradation ladder answered, when it was not the top one.
    degraded_to: str | None = None
    declined_out_of_scope: bool = False
    asked_question: bool = False
    final_tool_used: bool = False
    ids_removed: int = 0
    urls_removed: int = 0
    trimmed: bool = False
    flag: str | None = None
    exhausted: bool = False
    failed: bool = False
    duration_ms: int = 0

    def emit(self) -> None:
        """Write the turn. Never raises: telemetry must not cost anyone their reply."""
        try:
            log.info(
                "agent_turn",
                fast_path=self.fast_path,
                rounds=self.rounds,
                tool_calls=self.tool_calls,
                tools_used=len(self.tool_calls),
                cards_in_context=self.cards_in_context,
                degraded_to=self.degraded_to,
                declined_out_of_scope=self.declined_out_of_scope,
                asked_question=self.asked_question,
                final_tool_used=self.final_tool_used,
                guard={
                    "ids_removed": self.ids_removed,
                    "urls_removed": self.urls_removed,
                    "trimmed": self.trimmed,
                    "flag": self.flag,
                },
                exhausted=self.exhausted,
                failed=self.failed,
                duration_ms=self.duration_ms,
                prompt_version=PROMPT_VERSION,
            )
        except Exception as exc:  # noqa: BLE001 - a log line is never worth a turn
            log.warning("agent_trace_failed", error=type(exc).__name__)
