"""Did that actually save? Answered by the database, never by the model.

This is the deterministic lane. Everything else in this package asks a model something
and then bounds what comes back; this asks a model *nothing*. "Is it saved?" is a
question about a row, and a row either exists with a status or it does not -- so the
answer is a lookup and a sentence, with no provider call anywhere in it.

The bug this exists for is worth naming, because it looks like a prompt problem and is
not. A save is acknowledged ("Saving...") and finished asynchronously by the worker; the
conversation lane that answered the follow-up has no vault access by design, so it said
what it honestly could -- "I can't check what you've saved" -- while the row sat there
`completed`. No prompt fixes that: the conversation lane is *given* no memories, on
purpose, and widening it to reach the vault would put an unbounded generator in front of
the one fact in this product that must never be guessed at.

So the rule this module enforces is a general one:

    Never ask the model whether something happened. Ask the system that did it.

Three consequences hold it in place:

* **Card columns only.** `recent_saves` loads what a card needs -- title, status, when --
  and never `content` or `item_metadata`. A status answer that dragged an article body
  across the wire would cost more than the capture did.
* **The wording is a table, not a generation.** There is no model call here, so there is
  nothing in the loop that could translate the reply; a script-keyed table is the only
  honest way to answer a Bengali question in Bengali. Same trade as the no-match sentence
  in `recall_chat`: deliberately short, English for anything unlisted, because a
  machine-translated sentence that reads as broken is worse than plain English at the
  moment someone is asking whether their thing survived.
* **`processing_error` is never echoed.** It is scrubbed on the way into the database and
  is still, at best, a provider's own phrasing about our infrastructure. The user needs
  to know it failed and what to do; the text belongs in the vault UI and the logs.

Times are **relative** ("2 minutes ago"), not clock times. Nothing in a chat surface
carries the sender's timezone, so an absolute time here would be this server's opinion
rendered as the user's -- wrong by hours, and wrong in a way that reads as authoritative.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.core.scripts import script_of
from app.models.base import ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.types import OutboundReply, TextBlock

#: How many of the newest memories to read. One would answer the question asked; a
#: handful is what lets the reply add "2 more still processing", which is the other half
#: of what someone means by "did it save" after sending three things in a row. Bounded
#: because this is one indexed page and must stay one round trip.
LOOKBACK = 5

#: Titles are model output derived from scraped pages and can be a paragraph. Clipped
#: here rather than at render time so every surface gets the same sentence length.
_MAX_TITLE = 70


class SaveStatusReader(Protocol):
    """The one vault read this lane needs, injected so the package stays free of SQL.

    `user_id` is a required positional argument for the same reason it is everywhere else
    in this package: the tenant predicate belongs in the repository, and a signature with
    nowhere to put an optional user is a signature a later caller cannot get wrong.
    """

    async def recent_saves(
        self, user_id: uuid.UUID, limit: int
    ) -> tuple[Sequence[VaultItem], int]: ...


@dataclass(frozen=True)
class _Phrasing:
    """Every sentence this lane can say, in one language."""

    nothing_yet: str
    #: Takes {title} and {when}.
    saved: str
    #: Takes {title} and {when}. The item is stored but had nothing readable in it.
    saved_unreadable: str
    #: Takes {title}.
    working: str
    #: Takes {title}.
    failed: str
    #: Takes {count}. Appended to any of the above.
    also_working: str
    just_now: str
    #: Each takes {n}.
    minutes_ago: str
    hours_ago: str
    days_ago: str


_ENGLISH = _Phrasing(
    nothing_yet="Nothing saved yet. Send me a link and it'll show up here.",
    saved="✅ Saved — “{title}” ({when}).",
    saved_unreadable=(
        "✅ Saved — “{title}” ({when}). There was no readable text in it, so there's "
        "no summary."
    ),
    working=(
        "⏳ Got it — “{title}” is still processing. I'll send the summary the moment "
        "it's done."
    ),
    failed=(
        "❌ “{title}” didn't go through — processing failed. Send it again, or retry it "
        "from your vault."
    ),
    also_working="\n\n{count} more still processing.",
    just_now="just now",
    minutes_ago="{n} min ago",
    hours_ago="{n}h ago",
    days_ago="{n}d ago",
)

#: Keyed by *script*, which is all `script_of` can honestly report -- so `devanagari`
#: answers a Marathi speaker in Hindi, and no amount of character counting improves on
#: that. Adding a language is one entry and should be written by someone who speaks it.
_PHRASINGS: dict[str, _Phrasing] = {
    "bengali": _Phrasing(
        nothing_yet="এখনও কিছু সেভ করা হয়নি। একটা লিংক পাঠান, এখানে দেখা যাবে।",
        saved="✅ সেভ হয়েছে — “{title}” ({when})।",
        saved_unreadable=(
            "✅ সেভ হয়েছে — “{title}” ({when})। এতে পড়ার মতো কোনো টেক্সট ছিল না, "
            "তাই সামারি নেই।"
        ),
        working=(
            "⏳ পেয়েছি — “{title}” এখনও প্রসেস হচ্ছে। হয়ে গেলেই সামারি পাঠিয়ে দেব।"
        ),
        failed="❌ “{title}” সেভ হয়নি — প্রসেস করতে ব্যর্থ হয়েছি। আবার পাঠান।",
        also_working="\n\nআরও {count}টি এখনও প্রসেস হচ্ছে।",
        just_now="এইমাত্র",
        minutes_ago="{n} মিনিট আগে",
        hours_ago="{n} ঘণ্টা আগে",
        days_ago="{n} দিন আগে",
    ),
    "devanagari": _Phrasing(
        nothing_yet="अभी तक कुछ भी सेव नहीं किया गया। एक लिंक भेजिए, यहाँ दिख जाएगा।",
        saved="✅ सेव हो गया — “{title}” ({when})।",
        saved_unreadable=(
            "✅ सेव हो गया — “{title}” ({when})। इसमें पढ़ने लायक कोई टेक्स्ट नहीं था, "
            "इसलिए सारांश नहीं है।"
        ),
        working="⏳ मिल गया — “{title}” अभी प्रोसेस हो रहा है। होते ही सारांश भेज दूँगा।",
        failed="❌ “{title}” सेव नहीं हुआ — प्रोसेसिंग विफल रही। दोबारा भेजिए।",
        also_working="\n\n{count} और अभी प्रोसेस हो रहे हैं।",
        just_now="अभी",
        minutes_ago="{n} मिनट पहले",
        hours_ago="{n} घंटे पहले",
        days_ago="{n} दिन पहले",
    ),
}

#: The statuses that mean the pipeline has not reached a verdict yet. `pending` is in
#: here as well as `processing`: from the sender's side "queued" and "running" are the
#: same answer -- it arrived, it is not finished -- and the sweeper is what turns a
#: `pending` that never moved back into a real outcome.
_IN_FLIGHT = (ProcessingStatus.pending, ProcessingStatus.processing)


async def reply(
    reader: SaveStatusReader, user_id: uuid.UUID, message: str
) -> OutboundReply:
    """Read the newest saves for this user and say what became of them.

    `user_id` is the caller's already-resolved account. Nothing in the message chooses
    which rows are read -- there is no id, no title and no filter taken from it -- so
    this lane has no object to reference insecurely. The message is used for one thing:
    deciding which language to answer in.
    """
    items, _total = await reader.recent_saves(user_id, LOOKBACK)
    return OutboundReply([TextBlock(text=describe(items, message))])


def describe(items: Sequence[VaultItem], message: str = "", *, now: datetime | None = None) -> str:
    """The sentence for these rows. Pure, so the wording is testable without a database."""
    phrasing = _PHRASINGS.get(script_of(message) or "", _ENGLISH)
    if not items:
        return phrasing.nothing_yet

    latest = items[0]
    title = _title_of(latest)
    when = _relative(latest.created_at, phrasing, now=now)

    if latest.processing_status is ProcessingStatus.completed:
        line = phrasing.saved.format(title=title, when=when)
    elif latest.processing_status is ProcessingStatus.skipped:
        # Stored and downloadable, never sent to a model -- an image with no vision key,
        # a .docx with no parser. Saying "saved" alone would promise a summary that is
        # never coming; saying "failed" would suggest the file is gone, and it is not.
        line = phrasing.saved_unreadable.format(title=title, when=when)
    elif latest.processing_status is ProcessingStatus.failed:
        line = phrasing.failed.format(title=title)
    else:
        line = phrasing.working.format(title=title)

    # Counted over the rest, not the whole page: the newest one has just been described
    # in its own sentence and must not also be counted as "more".
    others = sum(1 for item in items[1:] if item.processing_status in _IN_FLIGHT)
    if others:
        line += phrasing.also_working.format(count=others)
    return line


def _title_of(item: VaultItem) -> str:
    """What to call this memory. Never `content` -- it is not loaded on a card query."""
    raw = (item.title or item.source_url or "").strip()
    if not raw:
        return "your last save"
    return raw if len(raw) <= _MAX_TITLE else raw[: _MAX_TITLE - 1].rstrip() + "…"


def _relative(moment: datetime | None, phrasing: _Phrasing, *, now: datetime | None = None) -> str:
    """How long ago, in the reply's own language.

    A row written by this application always has `created_at`, but the column is
    nullable and a card query is the one place a half-written row could surface, so
    None answers "just now" rather than raising in a lane whose whole job is to be the
    thing that still works.
    """
    if moment is None:
        return phrasing.just_now
    # `timestamptz` round-trips as aware; a naive value can only come from a fixture, and
    # subtracting it from an aware `now` would raise.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = max(0.0, ((now or datetime.now(UTC)) - moment).total_seconds())
    if seconds < 90:
        return phrasing.just_now
    if seconds < 3600:
        return phrasing.minutes_ago.format(n=int(seconds // 60))
    if seconds < 86400:
        return phrasing.hours_ago.format(n=int(seconds // 3600))
    return phrasing.days_ago.format(n=int(seconds // 86400))
