import logging
from pathlib import Path

import pymupdf

from app.core.chunking import Chunk, chunk_located
from app.core.parsers import image_parser

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = 20  # a page with less text than this is treated as scanned
OCR_DPI = 150
MAX_OCR_PAGES = 60  # bounds indexing time for a long scanned document


class LockedPdf(Exception):
    """The PDF needs a password to open (e-Aadhaar, PAN, bank statements)."""


# Passwords the user typed in chat ("the password is ..."), for this run of the app only: never written to disk,
# never sent to the model. Keyed by the file's full path.
_PASSWORDS: dict[str, str] = {}


def unlock(path: Path | str, password: str) -> bool:
    """True (and remembered until the app closes) if the password opens the file."""
    try:
        with pymupdf.open(str(path)) as doc:
            if not doc.needs_pass:
                return True
            if doc.authenticate(password):
                _PASSWORDS[str(path)] = password
                return True
    except Exception:
        return False
    return False


def is_locked(path: Path | str) -> bool:
    try:
        with pymupdf.open(str(path)) as doc:
            return bool(doc.needs_pass) and not (str(path) in _PASSWORDS and doc.authenticate(_PASSWORDS[str(path)]))
    except Exception:
        return False


def parse_pdf(path: Path) -> list[Chunk]:
    """One or more chunks per non-blank page, each citing its 1-indexed page number.
    Scanned pages (no text layer) are OCR'd when OCR is installed. Raises LockedPdf for a password-protected
    file the user hasn't unlocked in chat."""
    chunks: list[Chunk] = []
    ocr_ok = image_parser.available()
    ocr_pages = 0
    with pymupdf.open(path) as doc:
        if doc.needs_pass and not (str(path) in _PASSWORDS and doc.authenticate(_PASSWORDS[str(path)])):
            raise LockedPdf(f"{Path(path).name} is password-protected")
        for i, page in enumerate(doc, start=1):
            text = page.get_text()
            if len(text.strip()) < MIN_TEXT_CHARS and ocr_ok and ocr_pages < MAX_OCR_PAGES:
                ocr_pages += 1
                try:
                    text = image_parser.ocr(page.get_pixmap(dpi=OCR_DPI).tobytes("png")) or text
                except Exception as exc:
                    log.warning("OCR failed on %s page %d: %s", path, i, exc)
            chunks.extend(chunk_located("page", i, text))
    return chunks
