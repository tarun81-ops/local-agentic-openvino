"""Text for images, from two optional sources:

- OCR with RapidOCR (ONNX, CPU-only), when rapidocr_onnxruntime is installed. Also used for
  scanned PDF pages that have no text layer (see pdf_parser).
- A one-sentence caption from the local vision model (qwen3-vl), when CAPTION_IMAGES=true.
  Off by default: on a CPU it costs several seconds per image while indexing.

With neither, images are not indexed (see parsers/__init__.py). See docs/decisions/0001-images.md."""
import base64
import logging
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from app.config import settings
from app.core import personalize
from app.core.chunking import Chunk, chunk_located

log = logging.getLogger(__name__)

# Set by the indexer around one file: True/False overrides CAPTION_IMAGES for that file's folder.
CAPTION_OVERRIDE: ContextVar[bool | None] = ContextVar("caption_override", default=None)
MAX_CAPTION_BYTES = 8 * 1024 * 1024  # bigger images are OCR-only
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".bmp": "image/bmp"}
CAPTION_PROMPT = "Describe this image in one factual sentence for a file search index. Mention any visible names, dates or numbers."


@lru_cache(maxsize=1)
def _engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def ocr(image) -> str:
    """image: a path, or encoded image bytes (e.g. a rendered PDF page)."""
    result, _ = _engine()(image if isinstance(image, bytes) else str(image))
    return "\n".join(line[1] for line in (result or []))


def caption(path: Path) -> str:
    from app.core.llm import idle
    from app.core.llm.client import current_model, get_client

    data = path.read_bytes()
    if len(data) > MAX_CAPTION_BYTES:
        return ""
    url = f"data:{_MIME.get(path.suffix.lower(), 'image/png')};base64,{base64.b64encode(data).decode()}"
    idle.touch()
    response = get_client(local=True).chat.completions.create(  # the image bytes never go to the other server
        model=current_model(local=True),
        messages=[{"role": "user", "content": [{"type": "text", "text": CAPTION_PROMPT}, {"type": "image_url", "image_url": {"url": url}}]}],
        max_tokens=80,
    )
    return (response.choices[0].message.content or "").strip()


def parse_image(path: Path) -> list[Chunk]:
    parts = []
    override = CAPTION_OVERRIDE.get()
    if personalize.effective("caption_images") if override is None else override:
        try:
            text = caption(path)
            if text:
                parts.append(f"Image: {text}")
        except Exception as exc:  # the model being down must not fail OCR indexing
            log.warning("caption failed for %s: %s", path, exc)
    if available():
        parts.append(ocr(path))
    return chunk_located("image", 1, "\n".join(p for p in parts if p.strip()))
