"""One saved memory, rendered small enough to put many of them in a prompt.

A card is the *shortest description that still tells two memories apart*. That is the
whole design constraint. Retrieval hands back eight or ten items and the model has to
speak about them by name, so a card that reads "an article about jobs" is worth nothing
-- there are two hundred of those -- while the full body of the item is not affordable
ten times over. `ai_label` exists for exactly this and leads the card; `summary` is
clipped hard; `content` never appears at all.

**What is deliberately absent, and why**

* `content` -- the reason a card exists. It is the field that does not fit.
* `storage_key` -- the object key inside the private bucket. It grants nothing on its
  own, but it is never shown to a browser and there is no reason for a language model to
  be able to recite the bucket's layout either.
* `source_url` -- a card is about what the user kept, not where it came from. Leaving
  URLs out also means nothing here can be rendered as a link a person is invited to
  click, in text that came off a scraped page.
* the embedding -- it lives on `VaultChunk`, not here, and it is numbers.

**Everything interpolated is untrusted.** Titles, summaries, tags and highlights are
model output derived from scraped pages; an Instagram caption saying "ignore previous
instructions" is a caption someone can write on purpose. Two consequences are enforced
here rather than assumed: every value is flattened to a single line, so nothing in an
item's own text can open a line that looks like the start of another card, and every
value is length-capped, so one long field cannot crowd out the other memories. Labelling
this as quoted material is the caller's job -- the fencing in `ai/chat/chain.py` is what
tells the model these blocks are data.

Pure and offline: no database, no model call, no knowledge of any surface.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.models.vault import VaultItem

#: Roughly a sentence and a half. Long enough to recognise the item, short enough that
#: ten of them still leave room for the question and the answer.
SUMMARY_LIMIT = 200
#: Highlights are verbatim sentences and can be arbitrarily long. Capped so one runaway
#: quote cannot eat the budget, and the ellipsis is left visible so a clipped quote never
#: reads as a whole one.
HIGHLIGHT_LIMIT = 160
MAX_TAGS = 3
MAX_HIGHLIGHTS = 2
#: Eight hex characters. Enough to name an item back to the caller and to tell two apart;
#: short enough not to dominate the card.
ID_CHARS = 8

#: Default context budget, in estimated tokens.
DEFAULT_BUDGET = 1200
#: Everything here is prose, where a token is about four characters. Deliberately a
#: cheap approximation and not a real tokenizer: this decides how many cards to include,
#: and being a little conservative costs one card while a tokenizer dependency costs a
#: model download at import time.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """A rough token count. See `_CHARS_PER_TOKEN` for why it is rough on purpose."""
    return len(text) // _CHARS_PER_TOKEN


def _one_line(value: object) -> str:
    """Collapse to a single line. This is what stops an item forging a second card."""
    return " ".join(str(value).split())


def _clip(value: str, limit: int) -> str:
    """Trim to `limit` characters at a word boundary, marking that it was trimmed."""
    if len(value) <= limit:
        return value
    cut = value[:limit]
    space = cut.rfind(" ")
    # Only honour the boundary if there is one reasonably near the end; a single
    # enormous word would otherwise collapse the summary to nothing.
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


def _field(value: object, limit: int) -> str | None:
    """One interpolated value: flattened, clipped, and dropped when it is empty."""
    if value is None:
        return None
    text = _clip(_one_line(value), limit)
    return text or None


def build_card(item: VaultItem) -> str:
    """Render one memory. Every field is optional; an empty item still yields a card."""
    name = _field(item.ai_label, SUMMARY_LIMIT) or _field(item.title, SUMMARY_LIMIT)
    lines = [f"[{short_id(item)}] {name or 'Untitled'}"]

    facts = []
    saved = _saved_on(item)
    if saved:
        facts.append(f"saved {saved}")
    category = _field(item.ai_category, 60)
    if category:
        facts.append(category)
    if facts:
        lines.append("  " + " · ".join(facts))

    tags = _tags(item)
    if tags:
        lines.append(f"  tags: {', '.join(tags)}")

    summary = _field(item.summary, SUMMARY_LIMIT)
    if summary:
        lines.append(f"  summary: {summary}")

    for quote in _highlights(item):
        # "quote", not "note": the product has its own `/note` and a `ContentType.note`,
        # and a line reading `note: "..."` invites the model to describe a scraped
        # sentence as something the user wrote themselves.
        lines.append(f'  quote: "{quote}"')

    return "\n".join(lines)


#: How much of an item's body a detail answer may see. Two of these is already more than
#: the whole default context, which is the point: it is paid for only when the question
#: asked for the words rather than for which memory it was.
DETAIL_CONTENT_LIMIT = 2000
#: And over how few items. Reading two memories closely beats skimming eight.
DETAIL_MAX_ITEMS = 2


def build_detail_card(item: VaultItem, limit: int = DETAIL_CONTENT_LIMIT) -> str:
    """The card, plus as much of the item's own text as the limit allows.

    For the one question a card cannot answer: "what did it actually say?". The card
    stays on top so the answer can still name the memory, and the body is appended under
    a label that says plainly it is the saved text -- an unlabelled wall of prose reads
    to the model as more instructions.

    An item with no stored body (an image, a `.docx` -- see the `skipped` status) simply
    yields its card. There is nothing further to show and saying so is the answer.
    """
    card = build_card(item)
    body = _clip(" ".join(str(item.content or "").split()), limit)
    if not body:
        return card
    return f"{card}\n  full text: {body}"


def build_context(
    items: Sequence[VaultItem], budget: int = DEFAULT_BUDGET
) -> str:
    """Cards, newest-first as given, until the budget runs out. Separated by blank lines.

    **The first card is always included**, however big it is. A retrieval that found one
    long memory and returned nothing because it did not fit is indistinguishable from a
    retrieval that found nothing at all, and the second is a lie. Better an over-budget
    prompt than an answer of "I couldn't find anything" about an item that exists.

    Stops rather than skips: cards arrive in relevance order, so continuing past the
    first one that does not fit would silently promote a weaker memory over a stronger
    one purely because it was shorter.
    """
    cards: list[str] = []
    used = 0
    for item in items:
        card = build_card(item)
        cost = estimate_tokens(card)
        if cards and used + cost > budget:
            break
        cards.append(card)
        used += cost
    return "\n\n".join(cards)


def short_id(item: VaultItem) -> str:
    """The handle a memory is known by inside a prompt, and nowhere else.

    Public because three places have to agree on it exactly: the card that carries it,
    the fence in `ai/chat/chain.py` that labels the block with it, and the validator that
    checks a citation in the model's answer against the ids it was actually given. Two
    implementations of this is a validator that approves an id it derived itself.

    Not a secret and not a capability -- it is a prefix of a UUID the owner already has,
    reaching nothing on its own.
    """
    return item.id.hex[:ID_CHARS] if item.id is not None else "unsaved"


def _saved_on(item: VaultItem) -> str | None:
    """The date alone. A time of day is noise at the resolution anyone remembers."""
    created = item.created_at
    return created.date().isoformat() if created is not None else None


def _tags(item: VaultItem) -> list[str]:
    raw = item.ai_tags or []
    out = []
    for tag in raw:
        cleaned = _field(tag, 40) if isinstance(tag, str) else None
        if cleaned:
            out.append(cleaned)
        if len(out) == MAX_TAGS:
            break
    return out


def _highlights(item: VaultItem) -> list[str]:
    raw = item.ai_highlights or []
    out = []
    for quote in raw:
        cleaned = _field(quote, HIGHLIGHT_LIMIT) if isinstance(quote, str) else None
        if cleaned:
            # A quote arrives verbatim from the content and may contain a double quote
            # of its own, which would close the one wrapping it here.
            out.append(cleaned.replace('"', "'"))
        if len(out) == MAX_HIGHLIGHTS:
            break
    return out
