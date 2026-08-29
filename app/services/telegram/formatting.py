"""Reply text for the bot.

Everything here produces Telegram HTML. Only `& < >` are special in that mode, which is
why it is used instead of MarkdownV2 -- MarkdownV2 makes 18 characters special, including
`.` and `-`, so one unescaped article title returns HTTP 400 and the user gets nothing.

Every interpolated value goes through `escape`. Titles, tags, categories and summaries
are model output derived from scraped pages, i.e. attacker-influenced text, and this is
the boundary where it becomes markup.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from html import escape as _html_escape

from app.core.config import settings
from app.models.base import ProcessingStatus
from app.models.vault import VaultItem

_MAX_TITLE = 120
_MAX_SUMMARY = 400
_MAX_TAGS = 5


def escape(value: object) -> str:
    """HTML-escape any value for Telegram's HTML parse mode."""
    return _html_escape(str(value), quote=False)


#: A leading list marker with nothing after it to be a list of. The chat prompt permits
#: a leading "-" for genuine lists, and the model applies it to single-sentence answers
#: too, so "Hii" comes back as "- Hey! Send me a link and I'll keep it." -- a bullet
#: point with one item, which reads as a rendering fault rather than an answer.
_LONE_BULLET_RE = re.compile(r"^[-*#]+\s+")


def chat_reply(text: str) -> str:
    """A conversational reply, escaped, minus a bullet marker it never needed.

    Only stripped when the reply is a *single line*. A multi-line answer that opens with
    "-" is an actual list, and eating its first marker would leave one item looking
    different from the rest.

    This trims presentation, never content: the marker has to be followed by whitespace,
    so "-5 degrees", "#1 pick" and "**bold**" are left exactly as written. Escaping is
    unchanged and still happens here -- `escape` is the boundary where model output
    becomes markup, and nothing may reach Telegram without crossing it.
    """
    stripped = text.strip()
    if "\n" not in stripped:
        stripped = _LONE_BULLET_RE.sub("", stripped, count=1).strip()
    return escape(stripped)


def _clip(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _title_of(item: VaultItem) -> str:
    return _clip(item.title or item.source_url or "Untitled", _MAX_TITLE)


def connect_markup() -> dict[str, object] | None:
    """A one-tap button to the connect page, for a sender we cannot identify.

    The URL is built from `FRONTEND_URL` and never from anything in the update: an
    inline button is a link the user is being invited to trust, so its target has to
    come from configuration only.

    Telegram rejects a button whose URL is not externally reachable (`BUTTON_URL_INVALID`),
    and a rejected `sendMessage` means the user gets *no* reply at all -- so a non-https
    frontend (the local default) degrades to plain text rather than losing the message.
    """
    url = settings.FRONTEND_URL.rstrip("/")
    if not url.startswith("https://"):
        return None
    return {
        "inline_keyboard": [[{"text": "Connect my account", "url": f"{url}/capture"}]]
    }


def welcome(bot_can_link: bool) -> str:
    if not bot_can_link:
        return (
            "👋 <b>RecallAI</b> — your second brain.\n\n"
            "Send me a link, a file or a thought and I'll save it, summarise it and "
            "tag it. Ask me later and I'll find it.\n\n"
            "First, connect your account — tap the button below, sign in, and you'll "
            "be sent straight back here."
        )
    return connected_help()


def connected_help() -> str:
    return (
        "✅ <b>Connected.</b>\n\n"
        "<b>Send me — saved automatically</b>\n"
        "• any link — a reel, a video, an article\n"
        "• a forwarded post\n"
        "• a PDF or a photo\n\n"
        "<b>Keep a thought</b>\n"
        "<code>/note ring the dentist on Monday</code>\n\n"
        "<b>Ask me about what you saved</b>\n"
        "• what did I save this week?\n"
        "• any cooking videos?\n"
        "• show my SaaS ideas\n\n"
        "Anything else you type is just chat — I won't save it.\n\n"
        "<b>Shortcuts</b>\n"
        "/recent — your last saves\n"
        "/help — this message\n"
        "/disconnect — unlink this chat"
    )


def not_linked() -> str:
    return (
        "This chat isn't connected to a RecallAI account, so there's nothing for me to "
        "save to yet.\n\nTap below to connect — it takes one tap and you'll come "
        "straight back here."
    )


def link_expired() -> str:
    """One message for every failed redemption.

    Whether the token was never real, already spent, or belongs to someone else is not
    distinguished: telling the holder of a link which of those is true is exactly the
    information they would need to work out what they are holding.
    """
    return (
        "That connection link isn't valid any more — they last a few minutes and work "
        "once.\n\nTap below for a fresh one."
    )


def link_taken() -> str:
    return (
        "This Telegram account is already connected to a different RecallAI account.\n\n"
        "Disconnect it there first, then try again."
    )


def disconnected() -> str:
    return (
        "Disconnected. I won't save anything from this chat any more.\n\n"
        "You can reconnect any time from RecallAI on the web."
    )


def note_usage() -> str:
    return (
        "Add the thought after the command, like:\n"
        "<code>/note ring the dentist on Monday</code>"
    )


def chat_unavailable() -> str:
    """No chat model configured: say so rather than quietly saving the message.

    The alternative -- treating unanswerable text as a note -- turns "hi" into a memory
    the user has to find and delete, which is exactly the surprise `/note` exists to
    avoid.
    """
    return (
        "I can't chat right now — no answer model is configured for this deployment.\n\n"
        "Send me a link or a file and I'll still save it, or use <code>/note</code> to "
        "keep a thought."
    )


def saving() -> str:
    return "Saving…"


def duplicate(item: VaultItem) -> str:
    return f"📌 Already in your vault — <b>{escape(_title_of(item))}</b>\n{_tag_line(item)}"


def note_saved(item: VaultItem) -> str:
    return f"📝 Noted — <b>{escape(_title_of(item))}</b>\nI'll tag it in a moment."


def stored_without_text(item: VaultItem) -> str:
    """An image or an Office file: stored and downloadable, but never read.

    Said plainly rather than replying "saved", because no summary or tags are coming and
    a promise that never arrives reads as a bug.
    """
    return (
        f"📎 Saved — <b>{escape(_title_of(item))}</b>\n"
        "It's in your vault and you can download it, but I can't read this file type "
        "yet, so there's no summary or tags."
    )


def voice_unsupported() -> str:
    return (
        "I can't listen to voice notes yet — nothing was saved.\n\n"
        "Send it as text or a link and I'll take it from there."
    )


def too_large(limit_mb: int) -> str:
    return f"That file is over {limit_mb} MB, which is Telegram's limit for bots."


def unsupported_file() -> str:
    return (
        "I can't store that file type. PDFs, text, CSV, JSON, Office documents and "
        "images all work."
    )


def result(item: VaultItem) -> str:
    """The message sent once the pipeline finishes."""
    if item.processing_status == ProcessingStatus.failed:
        return (
            f"⚠️ Couldn't process <b>{escape(_title_of(item))}</b>.\n"
            "It's saved, so nothing is lost — you can retry it from the web app."
        )

    lines = [f"✅ <b>{escape(_title_of(item))}</b>"]
    if item.summary:
        lines.append(escape(_clip(item.summary, _MAX_SUMMARY)))
    meta = _tag_line(item)
    if meta:
        lines.append(meta)
    return "\n\n".join(lines)


def _tag_line(item: VaultItem) -> str:
    parts = []
    if item.ai_category:
        parts.append(f"🏷 <b>{escape(item.ai_category)}</b>")
    tags = [t for t in (item.ai_tags or []) if isinstance(t, str)][:_MAX_TAGS]
    if tags:
        parts.append(" ".join(f"<code>{escape(t)}</code>" for t in tags))
    return "\n".join(parts)


def recent(items: Sequence[VaultItem], total: int) -> str:
    if not items:
        return "Nothing saved yet. Send me a link and it'll show up here."
    lines = [f"🗂 <b>Your last {len(items)} of {total}</b>", ""]
    for item in items:
        title = escape(_title_of(item))
        line = f"• <b>{title}</b>" if not item.source_url else (
            f'• <a href="{escape(item.source_url)}">{title}</a>'
        )
        if item.ai_category:
            line += f" — {escape(item.ai_category)}"
        lines.append(line)
    return "\n".join(lines)


def rate_limited() -> str:
    return (
        "That's a lot at once — give it an hour and I'll pick up where we left off. "
        "Nothing was lost."
    )


def failed() -> str:
    return "Something went wrong on my side. Nothing was saved — try again in a minute."
