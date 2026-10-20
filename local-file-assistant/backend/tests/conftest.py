"""Test setup: every test run gets its own data dir and a fixed API token, and the OVMS
embedder is replaced by a deterministic bag-of-words hash so tests run without a model server."""
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

_DATA = Path(tempfile.mkdtemp(prefix="lfa-test-"))
os.environ.update(
    {
        "DATA_DIR": str(_DATA),
        "DB_PATH": str(_DATA / "index.db"),
        "ASSISTANT_DB_PATH": str(_DATA / "assistant.db"),
        "VECTOR_DB_DIR": str(_DATA / "lancedb"),
        "API_TOKEN": "test-token",
        "WATCH_FOLDERS": "",
        "AUTO_RESCAN": "false",
        "MIN_FREE_RAM_MB": "0",
        "RERANK_MODEL": "",  # tests that need one stand it in
        "STORE_FILE_TEXT": "true",  # most tests exercise the index; tests/test_names_only.py turns it off
    }
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import app.core.indexer as _indexer  # noqa: E402
import app.core.search.vector_search as _vector_search  # noqa: E402

REAL_EMBED = _vector_search.embed  # for tests that talk to a real OVMS
CORPUS = Path(__file__).resolve().parents[2] / "test_corpus"
DIM = 64


def fake_embed(texts, timeout=None):
    vectors = []
    for text in texts:
        if text.startswith("Instruct: ") and "\nQuery:" in text:  # a real embedder reads the instruction, it isn't content
            text = text.split("\nQuery:", 1)[1]
        v = [0.0] * DIM
        for word in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        vectors.append([x / norm for x in v])
    return vectors


@pytest.fixture(autouse=True)
def _no_model_server(monkeypatch):
    monkeypatch.setattr(_indexer, "embed", fake_embed)
    monkeypatch.setattr(_vector_search, "embed", fake_embed)


@pytest.fixture
def corpus(tmp_path) -> Path:
    """A private copy of test_corpus/ that tests may move files around in."""
    dest = tmp_path / "corpus"
    dest.mkdir()
    for f in CORPUS.iterdir():
        if f.suffix in {".pdf", ".docx"}:
            (dest / f.name).write_bytes(f.read_bytes())
    return dest


@pytest.fixture
def db_paths(tmp_path):
    return tmp_path / "index.db", tmp_path / "lancedb"


@pytest.fixture(autouse=True)
def _no_background_card_scan(monkeypatch):
    """The app starts the document-card scanner thread at startup (core/doc_photos.py). In tests it would read the
    same temporary folders as the test with real OCR and race it; tests call doc_photos.run_once() themselves."""
    from app.core import doc_photos

    monkeypatch.setattr(doc_photos.PhotoWorker, "start", lambda self: False)
