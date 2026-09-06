"""Prompt text shared by every provider.

The older prompts (summary, tags, category) are written out inside each provider because
their wording is pinned by that provider's tests. New ones live here: the whole point of
the label is that it reads the same whichever provider produced it, and a prompt copied
into two files is a prompt that only gets edited in one.
"""
from __future__ import annotations

from app.core.config import settings
from app.core.languages import language_name

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


#: Which prompt text produced a turn. One constant for every prompt in the chat path,
#: bumped on any edit to any of them -- the point is not to version each string but to be
#: able to date a regression, and a per-prompt version is a set of numbers nobody keeps
#: straight. Logged on every turn.
#:
#: v2: the snapshot gained both links per memory, `QueryMemories` replaced the fixed
#: search/list pair on the agent lane, and the prompt gained "never say you cannot give
#: them something without looking first" -- after a live turn answered "I can't provide
#: links directly" about memories that had two apiece.
#:
#: v3: bare URLs, list everything you were given, and say when a result was truncated --
#: after a live turn wrote `[Link](url)` into a chat that renders plain text, and reported
#: eight of thirteen memories as the whole vault.
PROMPT_VERSION = "agent-v3"

#: What the product can and cannot do, in the model's own context so it stops guessing.
#: Ten lines, no surface named: the same card is read by the bot and by the web page, and
#: a card that says "send me a link on Telegram" is wrong on one of them. Written as
#: capability rather than as marketing -- a model handed adjectives produces a sales pitch
#: when someone asks what it is.
CAPABILITY_CARD = """<capability_card>
The person saves links, files, notes and voice recordings. Each saved item is read,
summarised, tagged and made searchable; that takes a few seconds to a few minutes.
You can: list what they saved, search it by meaning, open one item and read it in full,
and say whether a recent capture finished, is still working, or failed.
You cannot: save, edit, delete or retry anything yourself; open the web; read anything
outside this person's own vault; or see an item that is still being processed in full.
A capture that is still working has no summary or tags yet -- that is normal, not a
failure. Say so plainly rather than reporting it as missing.
</capability_card>"""


def language_rule(subject: str = "it") -> str:
    """The one sentence every enrichment prompt uses to fix its output language.

    Written once and shared by four prompts -- the combined enrichment, both providers'
    summary and tags, and the label -- because the rule they each carried by hand was a
    rule that could be edited in one of them. `subject` only supplies the pronoun, so a
    tags prompt reads "Write them ..." rather than "Write it ...".

    `ENRICHMENT_LANGUAGE` is validated at boot against the closed list in
    `core/languages.py`, and what lands in the prompt is the *name* that list maps the
    code to -- never the configured string itself. That is what keeps this a template
    with a known set of fillings rather than a sentence an operator (or anything that
    reaches an environment variable) can finish.

    Read at call time rather than frozen into a module constant, so a test can pin either
    behaviour without depending on import order.
    """
    configured = settings.ENRICHMENT_LANGUAGE
    if configured == "content":
        # The older behaviour, and the right one for a person whose own notes are not in
        # English: a Bengali note summarised in English is a memory its author reads in
        # translation.
        return f"Write {subject} in the SAME LANGUAGE as the content."
    name = language_name(configured) or "english"
    return (
        f"Write {subject} in {name.title()}, whatever language the content is in — "
        "translate if the content is in another language."
    )


def label_prompt(text: str) -> str:
    """Ask for the one line that distinguishes this memory from every other one."""
    return (
        "Name this specific saved item in 3 to 7 words, the way a person would name it "
        "in a reading list. Be concrete: say what it is actually about — the product, "
        "place, method, claim or number at its centre. A generic subject area "
        '("technology", "career advice") is wrong; two different items must never get '
        "the same name. No quotes, no trailing period, no prefix like 'Title:'. "
        # The label is the line that tells two memories apart in a list, so it has to be
        # readable by the person whose list it is.
        + language_rule()
        + "\n\n"
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
