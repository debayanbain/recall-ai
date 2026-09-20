"""The sites a carousel names, pulled out of what the slides say.

A "top 10 websites" carousel is a list of destinations, and until now they were prose in
the middle of a 14,000-character body: `relocate.me` and `Jobbörse.de` were *in* the
vault and searchable, but there was no way to see at a glance what the post actually
pointed at, and nothing to click.

Three things about this are decisions, and the first is the one that matters.

**These are read from pixels, so they are claims, not links the author gave us.** A
scraped `<a href>` is a fact about a page; a domain a vision model read off a slide is its
best reading of some letters, and `j0blift.com` is one character from a squatter. So:

* the set is deliberately narrow -- a known public suffix, no IP literals, no userinfo,
  no port, no path, nothing that could carry a payload;
* `https://` is imposed rather than parsed out of the text, so nothing decides the scheme
  but us;
* and the UI that renders them says where they came from. A link presented as if the
  author wrote it, that goes somewhere they never named, is the way this feature could
  hurt somebody.

**Order is the slide's, deduped case-insensitively.** A carousel counts down "1. …
2. …", and a set would throw that away; the same site appearing on two slides is one
entry, at its first position.

**It never fails a capture.** Like the thumbnail and the slides themselves, this is
decoration over a memory that is already worth keeping.
"""
from __future__ import annotations

import re

#: A bare domain as written on a slide.
#:
#: Labels may hold non-ASCII letters, because the sites on these slides do: `Jobbörse.de`
#: is a real entry and an ASCII-only pattern silently drops it, which is the worst kind of
#: miss -- a link that is simply absent with nothing to say it was skipped. Deliberately
#: accepts no scheme, path, `@` or port: everything this yields is rebuilt as
#: `https://<host>` and nothing from the text survives into the URL but the host.
_LABEL = r"[^\W_](?:[^\W_]|-){0,61}[^\W_]|[^\W_]"
_DOMAIN = re.compile(rf"(?<![\w@/.])((?:{_LABEL})(?:\.(?:{_LABEL}))+)(?![\w@/])", re.UNICODE)

#: The endings that count as a site here. An allowlist rather than a blocklist, because
#: the input is prose *and* a list: "it is worth.it later" and "check.in tomorrow" both
#: parse as hosts under any general rule, and this module's own standard is that a missing
#: link beats a wrong one -- a chip that goes to a squatter is worse than no chip.
#:
#: So `.it` and `.in` are absent despite being real ccTLDs: they collide with English
#: words far more often than they appear as sites. **This is the knob** -- a carousel of
#: Italian or Indian job boards needs them added, and that is a deliberate edit rather
#: than a heuristic nobody can predict.
_TLDS = frozenset(
    """
    com net org info biz io co me dev app tech jobs work career careers
    eu de nl be at ch se dk no fi pl cz ie pt es fr it_not uk gov edu
    """.split()
) - {"it_not"}

#: A ceiling, because this runs over model output and a runaway transcription should not
#: write a hundred rows into a JSONB column.
MAX_LINKS = 60


def _plausible(host: str) -> bool:
    lowered = host.lower().rstrip(".")
    labels = lowered.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        return False
    if labels[-1] not in _TLDS:
        return False
    # Four dotted numbers is an IP literal, which is never a site somebody typed onto a
    # slide and is exactly what the SSRF guard exists to keep out of a fetch.
    return not any(label.isdigit() for label in labels)


def extract(text: str, *, limit: int = MAX_LINKS) -> list[str]:
    """Every site named in `text`, in first-seen order, as `https://<host>`."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _DOMAIN.finditer(text or ""):
        host = match.group(1).rstrip(".").lower()
        if not _plausible(host) or host in seen:
            continue
        seen.add(host)
        found.append(f"https://{host}")
        if len(found) >= limit:
            break
    return found
