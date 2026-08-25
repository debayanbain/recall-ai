"""PDF text extraction for uploads.

The binary is never stored. Blob storage (R2) is unconfigured, and for a memory vault the
*text* is what matters — it is what gets summarized, tagged and embedded. Extracting on
receipt and discarding the file keeps the feature independent of storage entirely.

Validation is deliberately not based on the filename or the browser's Content-Type, both
of which the client controls. A file is a PDF when its bytes start with `%PDF-`.
"""
from __future__ import annotations

import io

from app.core.logging import get_logger

log = get_logger("pdf")

PDF_MAGIC = b"%PDF-"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_PAGES = 200


class PdfError(ValueError):
    """The upload is not a usable PDF. Message is safe to show the user."""


def extract_text(data: bytes, filename: str | None = None) -> tuple[str, dict[str, object]]:
    """Return (text, metadata). Raises `PdfError` with an actionable message."""
    if not data:
        raise PdfError("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise PdfError(
            f"That file is {len(data) // 1_048_576}MB — the limit is "
            f"{MAX_UPLOAD_BYTES // 1_048_576}MB."
        )
    if not data.startswith(PDF_MAGIC):
        # Checked on bytes, not the extension: `report.pdf` can be anything.
        raise PdfError("That doesn't look like a PDF.")

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same to a user
        raise PdfError("We couldn't read that PDF — it may be corrupt.") from exc

    if reader.is_encrypted:
        # Attempting the empty-password unlock covers PDFs encrypted for permissions only.
        try:
            if reader.decrypt("") == 0:
                raise PdfError("That PDF is password protected.")
        except PdfError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PdfError("That PDF is password protected.") from exc

    pages = reader.pages[:MAX_PAGES]
    chunks: list[str] = []
    for page in pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one broken page must not lose the rest
            continue

    text = "\n\n".join(c.strip() for c in chunks if c.strip()).strip()
    if not text:
        # Almost always a scanned document: pages are images, so there are no glyphs.
        raise PdfError(
            "No text found — this looks like a scanned PDF, which needs OCR we don't do yet."
        )

    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS].rstrip()

    info: object = reader.metadata
    title = str(getattr(info, "title", "") or "").strip() or None
    if not title and filename:
        title = filename.rsplit("/", 1)[-1].removesuffix(".pdf").strip() or None

    return text, {
        "pages": len(reader.pages),
        "pages_read": len(pages),
        "truncated": truncated,
        "author": str(getattr(info, "author", "") or "") or None,
        "pdf_title": title,
        "source": "upload",
    }
