"""Prompt text shared by every provider.

The older prompts (summary, tags, category) are written out inside each provider because
their wording is pinned by that provider's tests. New ones live here: the whole point of
the label is that it reads the same whichever provider produced it, and a prompt copied
into two files is a prompt that only gets edited in one.
"""
from __future__ import annotations

LABEL_MAX_INPUT = 6000
HIGHLIGHTS_MAX_INPUT = 12000

#: Who the assistant is, in the three facts a person needs to use it. Kept here rather
#: than inline in a prompt because more than one surface has to answer "what are you?"
#: and two copies of an identity is how a product ends up describing itself two ways.
#: Deliberately plain: no emoji, no adjectives, nothing the model can expand into a
#: sales pitch. It states capability and stops.
BOT_IDENTITY = (
    "RecallAI is a memory assistant. "
    "It saves the links, files and notes a person sends it. "
    "It answers questions about what they have saved."
)


def label_prompt(text: str) -> str:
    """Ask for the one line that distinguishes this memory from every other one."""
    return (
        "Name this specific saved item in 3 to 7 words, the way a person would name it "
        "in a reading list. Be concrete: say what it is actually about — the product, "
        "place, method, claim or number at its centre. A generic subject area "
        '("technology", "career advice") is wrong; two different items must never get '
        "the same name. No quotes, no trailing period, no prefix like 'Title:'.\n\n"
        "CONTENT:\n" + text[:LABEL_MAX_INPUT]
    )


def highlights_prompt(text: str) -> str:
    """Ask for exact quotes, because anything else cannot be highlighted in place."""
    return (
        "Copy the 2 to 4 most important sentences from the content below, EXACTLY as "
        "they are written — character for character. Do not paraphrase, summarize, "
        "shorten, translate, merge two sentences, or fix spelling. Each string you "
        "return must appear in the content verbatim, or it will be discarded.\n"
        "Choose sentences carrying the substance a reader would want to find again: the "
        "claim, the instruction, the specific name or number. Skip greetings, hashtags, "
        "disclaimers and calls to follow or subscribe.\n"
        'Respond ONLY with a JSON array of strings, e.g. ["first sentence.", "second."]'
        "\n\nCONTENT:\n" + text[:HIGHLIGHTS_MAX_INPUT]
    )
