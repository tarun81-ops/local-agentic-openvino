import httpx

from app.config import settings
from app.db.vector_store import search as _table_search


def embed(texts: list[str], timeout: float = 120) -> list[list[float]]:
    """Embeddings from OpenVINO Model Server's /embeddings, in batches so one long document
    can't blow the timeout. OVMS keeps the embedder loaded, so batches don't reload it."""
    vectors: list[list[float]] = []
    size = max(1, settings.embed_batch_size)
    with httpx.Client(timeout=timeout) as client:
        for start in range(0, len(texts), size):
            batch = [t[: settings.embed_max_chars] for t in texts[start : start + size]]  # OVMS rejects, never cuts
            response = client.post(
                f"{settings.ov_base_url.rstrip('/')}/embeddings",
                json={"model": settings.embedding_model, "input": batch},
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda d: d["index"])
            vectors.extend(d["embedding"] for d in data)
    return vectors


def embed_query(texts: list[str], timeout: float = 120) -> list[list[float]]:
    """Embeddings for search questions. Qwen3-Embedding expects "Instruct: <task>\nQuery:<question>" for queries
    and nothing in front of documents; documents keep going through embed()."""
    instruct = settings.embed_query_instruct
    return embed([f"Instruct: {instruct}\nQuery:{t}" if instruct else t for t in texts], timeout=timeout)


def search(table, query: str, limit: int = 8, root: str | None = None) -> list[dict]:
    # A short timeout: search must stay usable when the embedder is slow; keyword results still come back.
    return _table_search(table, embed_query([query], timeout=15)[0], limit, root)
