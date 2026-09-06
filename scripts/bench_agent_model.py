"""Score candidate models on the turns this agent actually gets.

    uv run python scripts/bench_agent_model.py

Which model drives the agent loop is the largest single lever on how it behaves, and it
is not a question answerable from a model's reputation: the failure this lane was rebuilt
around was a model declining to *call a tool*, not a model getting a fact wrong. So this
scores the four shapes of turn that matter --

    links     the snapshot holds the answer  -> answer, do not search
    body      the snapshot does not          -> call the tool
    scope     not about the vault            -> decline in one line
    greeting  small talk                     -> do not run a search

-- and fails a model for what the prompt forbids: markdown, a needless tool call, a
refusal to hand over a link it was shown, a decline that runs long.

**It spends real money**, a handful of calls per model, and reaches a provider on
purpose. That is why it lives in `scripts/` rather than `tests/`: the autouse
`_no_provider_calls` fixture would refuse it, and rightly.

Findings as of 2026-09, three trials each: `gpt-4.1-mini` scored 4/4 every time at the
lowest output of any model that passed, which is why it is the configured
`AGENT_CHAT_MODEL`. `gpt-4.1` and `gpt-5.2` also score 4/4 and cost more per token for
the same answers here. `gpt-4o-mini` returns markdown. `gpt-5-mini` and `gpt-5-nano`
spend the whole output budget on reasoning tokens and return nothing -- raise
`_MAX_OUTPUT_TOKENS` a long way before trying that family.
"""
from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

import app.ai.chat.factory as factory
from app.ai.chat.harness.prompts import AGENT_SYSTEM
from app.ai.prompts import CAPABILITY_CARD

#: Add a candidate and re-run. Three trials of a shortlist tells a stable pass from a
#: lucky one; the failures above are not marginal and show up on the first attempt.
CANDIDATES = ["gpt-4.1-mini", "gpt-4.1", "gpt-5.2", "gpt-5-mini", "gpt-4o-mini"]


class QueryMemories(BaseModel):
    """Search or list the person's saved memories. You pick the filters and the fields."""

    text: str | None = Field(default=None, description="what to search for, by meaning")
    fields: list[str] = Field(
        default_factory=list,
        description="details to include: summary, tags, status, age, excerpt",
    )


SNAPSHOT = """<vault_snapshot trust="untrusted" total="12" showing="2">
<item id="592ca3cb" type="facebook" status="completed" age="27m" \
url="https://www.facebook.com/share/r/aaa111/" \
link="https://app.example/memory/592ca3cb-1111">Stop paying agency fees! You can apply \
directly for high-paying jobs in Switzerland</item>
<item id="9083b69e" type="facebook" status="completed" age="32m" \
url="https://www.facebook.com/share/r/bbb222/" \
link="https://app.example/memory/9083b69e-2222">AI ENGINEERING INTERVIEWS ARE \
CHANGING.</item>
</vault_snapshot>"""

#: `(name, what the person says, whether a tool call is the right move)`
CASES = [
    ("links", "Give me those links", False),
    ("body", "what did the switzerland one actually say?", True),
    ("scope", "Who is sunny leone?", False),
    ("greeting", "hey!", False),
]


def judge(case: str, wants_tool: bool, calls: list[str], text: str) -> list[str]:
    """What this reply got wrong. Empty means it passed."""
    faults: list[str] = []
    if wants_tool and not calls:
        faults.append("no tool call")
    if not wants_tool and calls:
        faults.append(f"needless {calls}")
    if "**" in text or "](" in text:
        faults.append("markdown")
    if case == "links":
        faults += [f"missing {u}" for u in ("aaa111", "bbb222") if u not in text]
        if "app.example/memory" not in text:
            faults.append("no vault link")
        if "can't" in text.lower() or "cannot" in text.lower():
            faults.append("refused")
    if case == "scope" and len(text) > 400:
        faults.append(f"decline ran to {len(text)} chars")
    return faults


async def run(model_id: str) -> tuple[str, list[tuple[str, list[str]]], int, int]:
    results: list[tuple[str, list[str]]] = []
    tokens_in = tokens_out = 0
    try:
        model = factory.build_chat_model("openai", model_id).bind_tools([QueryMemories])
    except Exception as exc:  # noqa: BLE001 - a candidate that cannot be built is a result
        return model_id, [("build", [f"{type(exc).__name__}: {exc!s:.60}"])], 0, 0

    for case, ask, wants_tool in CASES:
        messages = [
            SystemMessage(content=AGENT_SYSTEM),
            SystemMessage(content=f"{CAPABILITY_CARD}\n\n{SNAPSHOT}"),
            HumanMessage(content=ask),
        ]
        try:
            reply = await model.ainvoke(messages)
        except Exception as exc:  # noqa: BLE001 - so is a candidate that raises
            results.append((case, [f"{type(exc).__name__}: {exc!s:.60}"]))
            continue
        calls = [call["name"] for call in (getattr(reply, "tool_calls", None) or [])]
        text = reply.content if isinstance(reply.content, str) else str(reply.content)
        usage = reply.usage_metadata or {}
        tokens_in += usage.get("input_tokens", 0)
        tokens_out += usage.get("output_tokens", 0)
        results.append((case, judge(case, wants_tool, calls, text)))
    return model_id, results, tokens_in, tokens_out


async def main() -> None:
    scored = await asyncio.gather(*(run(model) for model in CANDIDATES))
    print(f"{'model':<14}{'pass':<7}{'in':>7}{'out':>7}   failures")
    print("-" * 78)
    for model_id, results, tokens_in, tokens_out in scored:
        passed = sum(1 for _, faults in results if not faults)
        failures = "; ".join(
            f"{case}: {', '.join(faults)}" for case, faults in results if faults
        )
        print(
            f"{model_id:<14}{passed}/{len(results)}    "
            f"{tokens_in:>7}{tokens_out:>7}   {failures or '-'}"
        )


if __name__ == "__main__":
    asyncio.run(main())
