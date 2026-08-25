"""Upload validation: what a document is allowed to be, and what it is called.

Every decision here is made from the *bytes*, never from the filename or the browser's
Content-Type -- both are attacker-controlled. `report.pdf` can be anything, and a client
that says `image/png` proves nothing.

The allowlist is closed on purpose. Two formats are missing that users will ask for:

* **SVG and HTML** are executable in a browser context. They are stored fine as bytes,
  but any path that ever renders them turns the bucket into a stored-XSS vector, so they
  are refused at the door rather than defended downstream.
* **Archives and executables** have no place in a memory vault and are the classic
  vehicle for everything else.

The object key is built here too, and never from user input: `users/<user>/<item>/<uuid>`
plus an extension this module chose. There is no component an upload can influence, so
`../` in a filename is not a traversal -- it is just a character the display name loses.
"""
from __future__ import annotations

import re
import unicodedata
import uuid

from app.core.config import settings
from app.services.pdf import PdfError
from app.services.pdf import extract_text as extract_pdf_text

#: Extension -> (mime type we will serve it as, magic-byte prefixes, is it text?).
#: An empty prefix tuple means "no signature": those are validated by decoding instead.
_ALLOWED: dict[str, tuple[str, tuple[bytes, ...], bool]] = {
    "pdf": ("application/pdf", (b"%PDF-",), False),
    "txt": ("text/plain", (), True),
    "md": ("text/markdown", (), True),
    "csv": ("text/csv", (), True),
    "json": ("application/json", (), True),
    "rtf": ("application/rtf", (b"{\\rtf",), False),
    # OOXML is a zip container. We never unzip it, so "is it really a docx" does not
    # matter -- a renamed zip is stored and handed back byte for byte, nothing parses it.
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        (b"PK\x03\x04",),
        False,
    ),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        (b"PK\x03\x04",),
        False,
    ),
    "pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        (b"PK\x03\x04",),
        False,
    ),
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",), False),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",), False),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",), False),
    "gif": ("image/gif", (b"GIF87a", b"GIF89a"), False),
    "webp": ("image/webp", (b"RIFF",), False),
    "heic": ("image/heic", (), False),
}

_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "heic"})

#: Anything outside this set is dropped from a display name. No path separators, no
#: control characters, no leading dots -- the name is shown in a UI and echoed in a
#: Content-Disposition header, and it is never part of the storage key.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._ \-()\[\]]+")
_MAX_NAME_LENGTH = 120
_MAX_TEXT_CHARS = 100_000


class DocumentError(ValueError):
    """The upload was refused. Message is written for a human and quotes no file bytes."""


def max_upload_bytes() -> int:
    return settings.MAX_UPLOAD_MB * 1024 * 1024


def allowed_extensions() -> list[str]:
    return sorted(_ALLOWED)


def _extension(filename: str | None) -> str:
    name = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return re.sub(r"[^a-z0-9]", "", ext)[:10]


def safe_display_name(filename: str | None, ext: str) -> str:
    """A filename fit to store, show and put in a download header.

    Unicode is normalised first so a name cannot smuggle a lookalike separator, then
    everything outside the allowlist collapses to `_`.
    """
    raw = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    normalised = unicodedata.normalize("NFKC", raw)
    cleaned = _UNSAFE_NAME_CHARS.sub("_", normalised).strip(" ._")
    if not cleaned:
        cleaned = f"upload.{ext}"
    if len(cleaned) > _MAX_NAME_LENGTH:
        stem, _, suffix = cleaned.rpartition(".")
        budget = _MAX_NAME_LENGTH - len(suffix) - 1
        cleaned = f"{stem[:budget]}.{suffix}" if stem else cleaned[:_MAX_NAME_LENGTH]
    if not cleaned.lower().endswith(f".{ext}"):
        cleaned = f"{cleaned}.{ext}"
    return cleaned


def object_key(user_id: uuid.UUID, item_id: uuid.UUID, ext: str) -> str:
    """`users/<user>/<item>/<random>.<ext>` -- every component is server-generated.

    Namespaced by user so a bucket listing is readable and a lifecycle rule can target
    one account, and randomised so the key cannot be guessed from the item id alone.
    """
    return f"users/{user_id}/{item_id}/{uuid.uuid4().hex}.{ext}"


class Document:
    """A validated upload: what it is, what to call it, what text it carries."""

    def __init__(
        self, *, data: bytes, ext: str, mime_type: str, display_name: str, is_image: bool
    ) -> None:
        self.data = data
        self.ext = ext
        self.mime_type = mime_type
        self.display_name = display_name
        self.is_image = is_image

    @property
    def size(self) -> int:
        return len(self.data)


def inspect(data: bytes, filename: str | None) -> Document:
    """Validate an upload. Raises `DocumentError` with an actionable message."""
    if not data:
        raise DocumentError("The file is empty.")

    limit = max_upload_bytes()
    if len(data) > limit:
        raise DocumentError(
            f"That file is {len(data) // 1_048_576}MB — the limit is {limit // 1_048_576}MB."
        )

    ext = _extension(filename)
    if ext not in _ALLOWED:
        raise DocumentError(
            "That file type isn't supported. Allowed: " + ", ".join(allowed_extensions()) + "."
        )

    mime, magic, is_text = _ALLOWED[ext]
    if magic and not any(data.startswith(prefix) for prefix in magic):
        # The extension claims one thing and the bytes say another.
        raise DocumentError(f"That file doesn't look like a real .{ext} file.")
    if ext == "webp" and data[8:12] != b"WEBP":
        # RIFF alone covers WAV and AVI too; the form type is what makes it a WebP.
        raise DocumentError("That file doesn't look like a real .webp file.")
    if is_text:
        _assert_decodable(data, ext)

    return Document(
        data=data,
        ext=ext,
        mime_type=mime,
        display_name=safe_display_name(filename, ext),
        is_image=ext in _IMAGE_EXTS,
    )


def _assert_decodable(data: bytes, ext: str) -> None:
    if b"\x00" in data[:8192]:
        # A NUL in the first pages means it is binary wearing a .txt extension.
        raise DocumentError(f"That .{ext} file doesn't look like text.")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentError(f"That .{ext} file isn't valid UTF-8 text.") from exc


def extract_text(document: Document) -> tuple[str | None, dict[str, object]]:
    """Pull whatever text the AI pipeline can use. `None` means "nothing to enrich".

    Only PDFs and plain-text formats yield text. An image or a .docx is stored and
    retrievable but not summarised: OCR and OOXML parsing are not built, and calling the
    model on an empty string would spend tokens to hallucinate about a filename.
    """
    meta: dict[str, object] = {
        "source": "upload",
        "file_name": document.display_name,
        "file_size": document.size,
        "mime_type": document.mime_type,
    }

    if document.ext == "pdf":
        try:
            text, pdf_meta = extract_pdf_text(document.data, document.display_name)
        except PdfError as exc:
            # A scanned or protected PDF is still worth *keeping* now that there is a
            # bucket -- the upload succeeds, it just carries no text to enrich.
            meta["text_extraction_error"] = str(exc)
            return None, meta
        meta.update(pdf_meta)
        return text, meta

    _, _, is_text = _ALLOWED[document.ext]
    if is_text:
        text = document.data.decode("utf-8", errors="replace").strip()
        if not text:
            return None, meta
        if len(text) > _MAX_TEXT_CHARS:
            text = text[:_MAX_TEXT_CHARS].rstrip()
            meta["truncated"] = True
        return text, meta

    return None, meta
