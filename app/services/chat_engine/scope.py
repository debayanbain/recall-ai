"""What this assistant is not: a general-purpose one. A closed gate, not a filter.

RecallAI answers about a person's own saved memories and about itself. Everything else --
who is this actress, what is Redis, write me a function, translate this -- is somebody
else's product, and answering it here costs three things worth more than the answer:

* **Trust.** A bot that fluently answers general trivia teaches the user that fluency is
  the signal. The next answer is about their vault, sounds exactly the same, and is now
  believed for the same reason. Everything else in this package exists to make a claim
  about the vault carry evidence; a confident answer about a film career sitting beside
  it, in the same voice, quietly erases the distinction.
* **The bill.** General conversation is unbounded and every turn is a provider call.
* **The surface area.** "Do X with this text" is the request shape every prompt-injection
  attempt takes. A bot that only talks about the vault has far less to be talked into.

**This module used to be a blocklist, and that was the bug.** It enumerated the shapes to
refuse -- "translate", "what is the capital of", "who is the president" -- and a live bot
asked "Who is sunny leone?" matched nothing and answered with a biography. Adding a
`who is <person>` pattern would not have fixed it: general knowledge is not a list of
phrasings, it is everything, and a blocklist over an infinite set is a hole with a few
planks nailed across it. The polarity is now inverted.

**The lane is closed by default.** A CHAT message is answered only if it is recognisably
one of three things, and is declined otherwise:

1. **Social.** A greeting, thanks, an acknowledgement, a goodbye. Matched against the
   *whole* normalised message, so "hi" passes and "hi, who is sunny leone" does not.
2. **Self-reference.** "how does this work", "what can you do" -- the assistant being
   asked about itself, which is in scope by definition.
3. **Domain.** The message names something this product is actually about: saving,
   notes, links, files, the vault, a source platform, connecting an account.

**Every pattern here is English, and that was a second hole.** Normalisation strips
everything outside `[a-z0-9' ]`, so a message in Bengali, Chinese or Arabic left an empty
string -- which this module read as "an emoji, so a reaction" and allowed. The result was
that every non-Latin sentence reached the conversation model, the one lane that cannot
see the vault, while the router's English question test had already declined to send it
to retrieval. Asking about your own memories in Bengali did nothing at all, quietly.
Non-Latin text longer than a greeting is now routed to retrieval by the router
(`app/core/scripts.py`), and anything unreadable that still arrives here is declined
rather than assumed friendly.

Everything else is declined with no model call. Note what is deliberately *not* a domain
signal: the words "you" and "your". They read as being about the bot, but "can you tell
me who X is" contains one, and admitting that single word reopens the whole hole.

The trade is deliberate and it runs the safe way round. A false decline costs a rephrase
and is visible to the user. A false allow is a confident answer in the assistant's own
voice that nobody notices. The blocklist had that trade backwards.

The prompt in `_CONVERSE_SYSTEM` still carries the same rule in prose -- two layers,
neither pretending to be complete -- and `recall_chat.chat` bounds what the model may
return even when both are talked past.

Only ever consulted for `Intent.CHAT`. A CAPTURE is being saved, a COMMAND is the
surface's own vocabulary, RECALL has already been recognised as a question about the
vault, and META is the assistant being asked about itself. So nothing here can intercept
a message that was going to become a memory or a search.

Pure and offline. `tests/chat_engine/test_scope.py` pins it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core import scripts

#: Bounded like the router's, and for the same reason: the caller may hand this an
#: arbitrarily long body, and the ask is in the opening in every case that matters.
_MAX_SCAN = 2000

#: Normalisation keeps letters, digits, spaces and apostrophes, so an emoji, a full stop
#: or three exclamation marks cannot stop "thanks!!" being recognised as thanks. Applied
#: before the anchored matches below, which is what lets them be anchored at all.
_KEEP_RE = re.compile(r"[^a-z0-9' ]+")


def _normalise(text: str) -> str:
    return " ".join(_KEEP_RE.sub(" ", text.lower()).split())


# --- 1. social ------------------------------------------------------------------------

#: Whole-message only. As substrings these would be a bypass in one step: "hi, who is
#: sunny leone" opens with a greeting and is not one.
_SOCIAL_RE = re.compile(
    r"^(?:"
    r"h+i+|h+e+y+|h+e+l+l+o+|hello|hola|yo|sup|"
    r"good (?:morning|afternoon|evening|night)|gn|gm|"
    r"how are (?:you|u)|how'?s it going|what'?s up|wassup|"
    r"(?:thanks|thank you|thank u|thankyou|ty|thx|tysm)"
    r"(?: (?:a lot|so much|very much|again|man|mate|bro))?|"
    r"o?k(?:ay)?|kk|cool|nice|great|awesome|perfect|got it|understood|i see|"
    r"y(?:es|eah|ep|up)|n(?:o|ope|ah)|sure|alright|fine|indeed|"
    r"bye|goodbye|see (?:you|ya)|cya|later|good night|"
    r"sorry|my bad|oops|"
    r"lol|lmao|rofl|ha(?:ha)+|he(?:he)+|hm+|"
    r"welcome|nice to meet (?:you|u)|please|pls"
    r")$"
)


# --- 2. self-reference ----------------------------------------------------------------

#: The assistant asked about itself. Most such questions are already `Intent.META`; these
#: are the phrasings the router's meta patterns do not carry. Anchored for the same
#: reason as the social set -- "what is this" is in scope, "what is this actress called"
#: is not.
_SELF_RE = re.compile(
    r"^(?:"
    r"what (?:is|are) this|what is it|what'?s this|"
    r"how (?:do|does) (?:this|it|that|you) work|"
    r"what (?:do|can) (?:you|u) do|"
    r"how (?:do|can) i (?:use|start with) (?:this|it|you)|"
    r"are (?:you|u) (?:there|ok|okay|working|alive)|"
    r"is (?:this|it) working|"
    r"what next|now what"
    r")$"
)


# --- 2b. a social message with a short tail -------------------------------------------

#: "Hii, thanks for that" is plainly conversation and plainly not a question, but it is
#: not a whole-message match for anything in the social set. Allowed under three
#: conditions together, none of which is sufficient alone: it is short, it *opens* with a
#: social word, and it asks for nothing.
#:
#: Opening position is doing real work. Anywhere-in-the-message was the first attempt and
#: it let "recommend a good movie" through on the word "good" -- a pleasantry leads a
#: sentence, it does not sit in the middle of a request.
_SOCIAL_WORD_RE = re.compile(
    r"^(?:h+i+|h+e+y+|h+e+l+l+o+|hello|thanks|thank|ty|thx|tysm|"
    r"ok|okay|cool|nice|great|awesome|perfect|good|morning|welcome|"
    r"bye|goodbye|sorry|lol|lmao|yes|yeah|yep|no|nope|sure|alright|fine|please)$"
)

#: The words that turn a pleasantry into a request. `who` is the one that mattered:
#: "hi, who is sunny leone" is six words with a greeting in front of it, and without this
#: it would ride in on the rule above. Deliberately no `is`/`are` -- "hi, is it working?"
#: is a fair question about the bot, and `who`/`what` already catch the dangerous form.
_ASKING_RE = re.compile(
    r"\b(?:who|what|when|where|why|how|which|whose|about|"
    r"tell|give|explain|name|define|describe|list|write|make|show|"
    r"recommend|suggest|need|want)\b"
)

#: Short enough that the social word is most of the message rather than a doorway into
#: it. "thanks! now tell me about the eiffel tower" is eight words and does not qualify
#: twice over.
_SOCIAL_TAIL_MAX_WORDS = 6


def _is_social_with_tail(normalised: str) -> bool:
    words = normalised.split()
    if not words or len(words) > _SOCIAL_TAIL_MAX_WORDS:
        return False
    if _ASKING_RE.search(normalised):
        return False
    return _SOCIAL_WORD_RE.match(words[0]) is not None


# --- 3. domain ------------------------------------------------------------------------

#: The vocabulary of the product itself. A message naming one of these is talking about
#: what this thing does, wherever the words sit in the sentence.
#:
#: `you` and `your` are deliberately absent -- see the module docstring. `summary` and
#: `summarise` are absent too: "summarise this article for me" is a general-assistant
#: request wearing a domain word, and a real question about a saved item's summary is
#: `Intent.RECALL` long before it reaches here.
#:
#: The names here are *content sources* -- where a saved thing came from. The messaging
#: surface this bot happens to run on is deliberately not among them: this package serves
#: every surface and may not name one, which `tests/chat_engine/test_boundaries.py`
#: enforces. Nothing is lost, because "how do I connect my account" already carries two
#: domain words of its own.
_DOMAIN_RE = re.compile(
    r"\b(?:"
    r"saves?|saved|saving|note|notes|link|links|url|vault|"
    r"memor(?:y|ies)|remember(?:ed|ing)?|bookmark(?:s|ed)?|"
    r"files?|pdfs?|photos?|images?|pictures?|documents?|docs?|"
    r"articles?|reels?|posts?|videos?|captures?|captured|uploads?|uploaded|"
    r"tags?|tagged|tagging|categor(?:y|ies)|folders?|"
    r"recall|recallai|instagram|facebook|youtube|tiktok|linkedin|twitter|"
    r"connects?|connected|connecting|disconnect(?:ed)?|account|"
    r"help|bot|assistant"
    r")\b"
)


# --- and the shapes refused even when they carry a domain word ------------------------

#: A general-assistant instruction does not become in-scope by mentioning a note.
#: "translate this note", "write a caption for my saved post" -- both name the domain and
#: neither is a question about the vault. Checked first, so a domain word cannot launder
#: them.
_BLOCKED_RE = re.compile(
    "|".join(
        (
            r"\b(?:write|create|generate|draft|compose)\b[^.?!]{0,40}?"
            r"\b(?:code|script|program|function|essay|poem|song|story|article|blog|"
            r"email|letter|caption|tweet|cover letter|resume|cv)\b",
            r"\btranslate\b",
            r"\b(?:debug|refactor|optimi[sz]e|fix)\b[^.?!]{0,20}"
            r"\b(?:code|script|function|bug|error)\b",
            r"\bact as\b|\bpretend (?:to be|you are|you're)\b|\brole[- ]?play\b",
            r"\bignore\b[^.?!]{0,25}\b(?:instructions|rules|prompt)\b",
            r"\b(?:show|reveal|repeat|print)\b[^.?!]{0,15}"
            r"\byour (?:system )?(?:prompt|instructions|rules)\b",
        )
    ),
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether the conversation lane may answer, and why -- the why is for the log.

    A decline that cannot say which gate refused it is a decline nobody can tune, and
    this is the gate most likely to need tuning against real messages.
    """

    allowed: bool
    reason: str


#: The reply. Plain sentences, no markup -- rendering belongs to the surface. It names the
#: boundary and then says what this *does*: a bare refusal reads as a fault and leaves the
#: person with nothing to try next, which is how a correct decline still feels like a
#: broken bot.
DECLINE = (
    "I'm a memory assistant, so I can't answer general questions. "
    "Send me a link, a file or a forwarded post and I'll save it, "
    "use /note to keep a thought, and ask me anything about what you've saved."
)


def check(text: str | None) -> Verdict:
    """Decide whether a CHAT message may reach the conversation model."""
    raw = (text or "").strip()[:_MAX_SCAN]
    if not raw:
        # Nothing to gate and nothing to answer. Allowed rather than declined so an
        # attachment-only message keeps whatever behaviour its surface gave it.
        return Verdict(True, "empty")

    if _BLOCKED_RE.search(raw):
        return Verdict(False, "blocked_shape")

    normalised = _normalise(raw)
    if not normalised:
        # `_normalise` keeps only `[a-z0-9' ]`, so "nothing left" covers two very
        # different messages and used to wave both through as social.
        #
        # An emoji or a run of punctuation really is a reaction. A sentence in another
        # script is not: "আমার নোট দেখাও" normalises to spaces too, and calling it a
        # greeting sent every Bengali message to a model that cannot see the vault. Only
        # something short enough to be a greeting keeps that treatment; anything longer
        # is refused here rather than answered, and the router now sends it to retrieval
        # before it can reach this gate at all.
        if scripts.is_short_reaction(raw):
            return Verdict(True, "social")
        return Verdict(False, "unreadable_script")
    if _SOCIAL_RE.match(normalised):
        return Verdict(True, "social")
    if _SELF_RE.match(normalised):
        return Verdict(True, "self_reference")
    if _is_social_with_tail(normalised):
        return Verdict(True, "social")
    if _DOMAIN_RE.search(normalised):
        return Verdict(True, "domain")

    # The default, and the whole point of the module: unrecognised is refused, not
    # forwarded. Everything a person actually says to a memory bot is one of the three
    # cases above; everything else is a question for a different product.
    return Verdict(False, "no_domain_signal")


def is_out_of_scope(text: str | None) -> bool:
    """`check`, as the boolean the engine branches on."""
    return not check(text).allowed
