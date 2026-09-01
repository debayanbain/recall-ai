"""Pulling links out of untrusted text, and refusing the ones that lie about themselves.

Every string this module reads is written by someone who is not our user: a YouTube
description, a caption, a model's transcription of text burned into a video frame, a
model's transcription of speech. The output is stored in `item_metadata` and rendered as
something a person is invited to tap. That combination is the whole reason this is its
own module with its own tests rather than one regex at a call site.

Three rules, in the order they matter:

* **Nothing here is ever fetched.** These are display links. The moment something calls
  `httpx.get` on one of them, this becomes a server-side request forgery primitive with
  a public trigger -- anyone who can get a user to save a reel controls a request from
  our worker. If a future feature does need to resolve one, it goes through
  `app/core/net.py::assert_safe_url` first, on every redirect hop, exactly like
  `ArticleExtractor` does.
* **The scheme is an allowlist, not a blocklist.** `javascript:`, `data:` and `vbscript:`
  are the obvious ones, but the set of URL schemes a browser or an OS will act on is not
  knowable, so anything that is not http or https is dropped without an opinion about
  what it was.
* **A link that renders as one host and resolves to another is refused, not cleaned.**
  `https://youtube.com@evil.com/` reads as YouTube in any UI that shows the start of a
  string, and Unicode homoglyphs (Greek omicron in `google.com`) read as the real thing
  at any length. Both are dropped: we cannot show someone a link whose destination we
  would have to explain.

`origin_label` exists for the same reason -- the UI must show the real host next to the
link text, so the decision to tap is made against the destination rather than against
whatever words happened to surround it in the frame.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

#: Schemes a link may carry. Everything else -- including `javascript:`, `data:`,
#: `vbscript:`, `file:` and every scheme invented after this was written -- is dropped.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Longest URL kept. Far past any real link; the point is that an unbounded string from
#: a model reaches a JSONB column and then a DOM attribute.
MAX_URL_CHARS = 512

#: RFC host limits, enforced because a 3000-character host is a rendering problem rather
#: than a link.
MAX_HOST_CHARS = 253
MAX_LABEL_CHARS = 63

# Invisible characters, and characters that terminate a URL early in one parser but not
# in another. Removed before anything is parsed, so tab/newline splitting and zero-width
# joiners inserted between homoglyphs cannot survive into the stored value.
#
# Chosen by Unicode *category* rather than by a literal character class: the literal form
# of this set is, by definition, a regex no reviewer can read, and one stray codepoint
# pasted into it is invisible in a diff. Cc is controls, Cf is formatting (zero-width
# joiners, bidi overrides, the BOM), Zs/Zl/Zp is every kind of space and separator.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp"})


def _strip_invisible(text: str) -> str:
    return "".join(
        ch for ch in text if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES
    )


# A scheme prefix of any kind, with or without the `//`. Matched separately from the URL
# regex so `javascript:alert(1)` is *recognised* as carrying a scheme and refused, rather
# than being mistaken for a bare host and handed an `https://` prefix.
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")

# Bare-domain and full-URL shapes. Deliberately conservative: a false negative costs one
# link, a false positive turns "see figure 2.png" into something clickable.
_URL_RE = re.compile(
    r"""
    (?<![\w@./-])                                       # not mid-word, not an email local part
    (?:
        (?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]{0,15}://)?    # optional scheme
        (?P<host>
            (?:[^\s/?\#@:.\x00-\x20]+\.)+               # one or more labels, each dot-ended
            [^\s/?\#@:.\x00-\x20]{2,24}                 # TLD-ish last label
        )
        (?P<port>:\d{1,5})?
        # Consumed, never kept. If a `@` follows the host then what came before it was
        # userinfo, not the destination -- matching only the leading half would hand
        # `normalise_link` a clean `youtube.com` and silently invent a link to it.
        (?P<userinfo>@[^\s<>"'`\]}]*)?
        (?P<rest>[/?\#][^\s<>"'`\]}]*)?                 # optional path / query / fragment
    )
    """,
    re.VERBOSE,
)

# Last labels that are almost always a file extension rather than a TLD. Only consulted
# for a bare token with no scheme and no path -- `report.pdf` in a caption is a filename,
# while `https://report.pdf/` is someone being difficult and goes through the normal
# checks. `.zip` and `.mov` really are TLDs, which is exactly why they are listed: a model
# reading `invoice.zip` off a frame has found a filename, not a website.
_FILE_EXTENSIONS = frozenset(
    {
        "avi", "bmp", "css", "csv", "doc", "docx", "exe", "gif", "gz", "heic", "html",
        "ico", "jpeg", "jpg", "js", "json", "log", "md", "mov", "mp3", "mp4", "otf",
        "pdf", "png", "ppt", "pptx", "py", "rar", "svg", "tar", "tiff", "ts", "tsx",
        "txt", "wav", "webm", "webp", "xls", "xlsx", "xml", "yaml", "yml", "zip",
    }
)

# Trailing punctuation that belongs to the sentence, not the URL. A link at the end of a
# caption routinely collects several of these.
_TRAILING = ".,;:!?)]}>\"'`»…"


class LinkRejected(ValueError):
    """This string is not a link we are willing to store or show."""


def extract_links(*texts: str | None, limit: int = 25) -> list[str]:
    """Find every safe http(s) link across `texts`, deduplicated, in first-seen order.

    First-seen rather than sorted on purpose: a creator's own link is usually the first
    one shown or said, and an alphabetical list buries it under whatever sponsor URL
    happened to start with an 'a'.
    """
    seen: set[str] = set()
    out: list[str] = []

    for text in texts:
        if not text:
            continue
        for match in _URL_RE.finditer(text):
            if len(out) >= limit:
                return out
            try:
                url = normalise_link(match.group(0))
            except LinkRejected:
                continue
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(url)

    return out


def normalise_link(raw: str) -> str:
    """Return a safe absolute http(s) URL, or raise `LinkRejected`.

    The returned string is rebuilt from parsed components rather than trimmed out of the
    input, for the same reason `services/editor_doc.py` re-serializes markup from an
    allowlist: a value assembled only from parts we understood cannot carry a part we
    did not.
    """
    candidate = _strip_invisible(raw or "")
    trimmed = candidate.rstrip(_TRAILING)
    # A ')' that closes a '(' inside the URL is part of the link -- Wikipedia does this
    # constantly. Restored by looking at what rstrip actually removed, not at what the
    # candidate ends with: `.../Foo_(bar).` loses both characters in one pass, so testing
    # the last character finds a '.' and puts the paren back on nothing.
    removed = candidate[len(trimmed):]
    if trimmed.count("(") > trimmed.count(")") and ")" in removed:
        trimmed += ")"
    candidate = trimmed

    if not candidate or len(candidate) > MAX_URL_CHARS:
        raise LinkRejected("empty or over-long")

    scheme_match = _SCHEME_RE.match(candidate)
    if scheme_match:
        # Checked here rather than after urlsplit so a scheme with no `//` -- which is
        # every dangerous one -- is refused instead of being read as a bare hostname.
        if scheme_match.group(1).lower() not in ALLOWED_SCHEMES:
            raise LinkRejected(f"scheme {scheme_match.group(1)!r} is not renderable")
        bare = False
    else:
        bare = True
        # A protocol-relative `//host/path` inherits the current page's scheme, which is
        # precisely the ambiguity being removed here.
        candidate = "https://" + candidate.lstrip("/")

    try:
        parts = urlsplit(candidate)
        port = parts.port
        hostname = parts.hostname
    except ValueError as exc:
        # urlsplit raises on a malformed port or an unbalanced IPv6 bracket, and it does
        # so lazily -- from `.port`, not from the split itself.
        raise LinkRejected("URL does not parse") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise LinkRejected(f"scheme {parts.scheme!r} is not renderable")

    # `https://good.com@evil.com/` renders as good.com wherever a UI truncates, and
    # navigates to evil.com. There is no legitimate userinfo in a link read off a video
    # frame, so this is a refusal rather than a repair.
    if "@" in parts.netloc:
        raise LinkRejected("URL carries userinfo")

    host = _safe_host(hostname)

    suffix = ""
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        # Default ports are dropped so two spellings of one link dedupe against each
        # other; anything else is kept, and shown.
        suffix = f":{port}"

    if bare and not parts.path and not parts.query:
        # A bare token with no path: check it is not just a filename someone wrote in a
        # caption. With a scheme or a path present the author clearly meant a URL.
        if host.rsplit(".", 1)[-1] in _FILE_EXTENSIONS:
            raise LinkRejected("looks like a filename, not a host")

    rebuilt = urlunsplit((scheme, host + suffix, parts.path, parts.query, parts.fragment))
    if len(rebuilt) > MAX_URL_CHARS:
        raise LinkRejected("over-long after normalisation")
    return rebuilt


def _safe_host(hostname: str | None) -> str:
    """Validate a hostname and return it lowercased, or raise.

    Non-ASCII hostnames are refused outright rather than punycoded. Punycoding one makes
    it *fetchable* and leaves it *unreadable*: `xn--ggle-0nda.com` and `google.com` are
    different sites that nobody reading a link list can tell apart, and letting them
    decide where they are going is the entire job of that list. A genuine
    internationalised domain in a video description is a loss taken knowingly; the
    homoglyph domain in a scam reel is the common case here.
    """
    if not hostname:
        raise LinkRejected("URL has no host")

    host = hostname.strip().rstrip(".").lower()
    if not host or len(host) > MAX_HOST_CHARS:
        raise LinkRejected("host is empty or over-long")

    # NFKC first: without it a fullwidth character normalises *later*, inside some other
    # library, into something different from what was validated here.
    if not host.isascii() or unicodedata.normalize("NFKC", host) != host:
        raise LinkRejected("host contains non-ASCII characters")

    labels = host.split(".")
    if len(labels) < 2:
        # No dot means an intranet name, or a bare word the regex over-matched. Neither
        # is worth showing, and an intranet name is one nobody should be invited to open.
        raise LinkRejected("host is not a dotted name")

    for label in labels:
        if not label or len(label) > MAX_LABEL_CHARS:
            raise LinkRejected("bad host label")
        if label.startswith("xn--"):
            # The already-encoded form of the same problem. Rejecting the non-ASCII
            # spelling while accepting `xn--ggle-0nda.com` would be a check that only
            # catches the attacker who did not press submit twice.
            raise LinkRejected("host is an internationalised (punycode) domain")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label):
            raise LinkRejected("bad host label")

    if not re.fullmatch(r"[a-z]{2,24}", labels[-1]):
        # Rejects a bare IP address too, which is intended: a numeric host in a caption
        # is never a link a creator meant a viewer to type.
        raise LinkRejected("last label is not a TLD")

    return host


def origin_label(url: str) -> str:
    """The host a UI shows beside the link, so the destination is what gets read.

    Returns an empty string rather than raising: it is called at render time on a value
    that already passed `normalise_link`, and a display helper must not be able to break
    a page.
    """
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def collect_links(
    sources: list[tuple[str, str | None]], limit: int = 25
) -> list[dict[str, str]]:
    """Extract links from several texts at once, tagging each with where it came from.

    `sources` is ordered by trust, most trusted first, and the first source to yield a
    link is the one recorded against it. That ordering is the whole point: a URL typed by
    a creator into a description is a fact, while the same URL read by a model off a
    blurry frame or transcribed from speech is a guess that happens to be right. The UI
    has to be able to say which it is showing, because a misread character in a domain is
    the difference between a link and a phishing page.

    Returns `[{"url": ..., "source": ...}]` -- a shape a template can render without
    re-deriving anything, and one that survives a JSONB round trip.
    """
    seen: set[str] = set()
    out: list[dict[str, str]] = []

    for source, text in sources:
        if not text or len(out) >= limit:
            continue
        for url in extract_links(text, limit=limit):
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"url": url, "source": source})
            if len(out) >= limit:
                break

    return out
