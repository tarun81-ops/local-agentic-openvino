"""Measures answer quality on the real local model, so model, prompt or chunking changes can
be compared with numbers instead of impressions.

    cd backend
    .venv\\Scripts\\python.exe eval\\run_eval.py                       # test_corpus + eval\\questions.jsonl
    .venv\\Scripts\\python.exe eval\\run_eval.py --folder "D:\\Docs" --questions my_questions.jsonl
    .venv\\Scripts\\python.exe eval\\run_eval.py --model qwen3-vl:4b-instruct

Each line of the questions file is JSON:
    {"q": "...", "expect": [["4,250", "4250"]], "file": "invoice_notes.pdf"}
  expect: every inner list must have at least one alternative in the answer (case-insensitive).
  file:   the file the answer should come from (retrieval + citation are checked against it).
  {"q": "...", "no_answer": true}  -> the files don't contain this; the model should say so.
  {"q": "...", "no_answer": true, "mode": "files_only"}  -> FILES ONLY: refused even if the model knows it.

Uses a throwaway index (never your real one) and needs OpenVINO Model Server running with both models exported.
Writes eval\\results\\<time>.json and prints a summary.

Retrieval only (no chat model, a few minutes): which file each search stage ranks first.
    .venv\\Scripts\\python.exe eval\\run_eval.py --retrieval       # eval\\retrieval_corpus.py + retrieval_questions.jsonl
    .venv\\Scripts\\python.exe eval\\run_eval.py --retrieval --folder "D:\\Docs" --questions mine.jsonl
  Lines: {"q": "...", "file": "name.pdf" or ["a.pdf", "b.docx"], "kind": "keyword"}"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
REFUSAL_RE = re.compile(r"\b(not (found|mentioned|contain|include|provide|available)|no (information|mention)|doesn't|does not|don't|do not|cannot|can't|unable)\b", re.I)


def score(item: dict, answer: str, chunks: list[dict], verdict: dict) -> dict:
    """Scores one question. Pure function: covered by tests/test_eval.py."""
    cited_files = {Path(c.get("path") or c["file"]).name.lower() for c in verdict["citations"]}
    retrieved = [Path(c["path"]).name.lower() for c in chunks]
    if item.get("no_answer"):
        refused = bool(REFUSAL_RE.search(answer))
        verified_claims = any(c["verified"] for c in verdict["citations"])
        return {"correct": refused and not verified_claims, "refused": refused, "hallucinated_citation": verified_claims}
    want = item.get("file", "").lower()
    hits = [any(alt.lower() in answer.lower() for alt in alts) for alts in item.get("expect", [])]
    return {
        "correct": all(hits) if hits else None,
        "retrieved_expected_file": (want in retrieved) if want else None,
        "top1_expected_file": (retrieved[:1] == [want]) if want else None,
        "cited_expected_file": (want in cited_files) if want else None,
        "all_citations_verified": verdict["all_verified"],
    }


def relevance(question: str, answer: str, chunks: list[dict], verdict: dict) -> float | None:
    """Heuristic for the verifier gap: a cited, verified answer can still miss the question. The
    share of the question's content words found in the answer, the cited file names and the cited
    passages; low values mark answers worth reading. None when the question has no content words."""
    from app.core.search import fts_search

    terms = fts_search.terms(question)
    if not terms:
        return None
    cited = {Path(c.get("path") or c["file"]).name.lower() for c in verdict["citations"]}
    cited_text = " ".join(f"{Path(c['path']).name} {c['text']}" for c in chunks if Path(c["path"]).name.lower() in cited)
    haystack = f"{answer} {cited_text}".lower()
    return round(sum(t in haystack for t in terms) / len(terms), 2)


def summarize(rows: list[dict]) -> dict:
    def rate(key, subset=None):
        vals = [r["score"][key] for r in (subset or rows) if r["score"].get(key) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    answerable = [r for r in rows if not r["item"].get("no_answer")]
    traps = [r for r in rows if r["item"].get("no_answer")]
    rel = [r["relevance"] for r in answerable if r.get("relevance") is not None]
    return {
        "questions": len(rows),
        "answer_correct": rate("correct", answerable),
        "retrieval_hit": rate("retrieved_expected_file", answerable),
        "retrieval_top1": rate("top1_expected_file", answerable),
        "citation_correct": rate("cited_expected_file", answerable),
        "citations_verified": rate("all_citations_verified", answerable),
        "answer_relevance": round(sum(rel) / len(rel), 3) if rel else None,
        "no_answer_handled": rate("correct", traps),
        "median_first_token_s": _median([r["first_token_s"] for r in rows if r["first_token_s"] is not None]),
        "median_total_s": _median([r["total_s"] for r in rows]),
    }


def _median(xs):
    xs = sorted(xs)
    return round(xs[len(xs) // 2], 2) if xs else None


STAGES = ("keyword", "semantic", "hybrid")
ORIGINAL_TYPES = {".pdf", ".docx", ".pptx", ".xlsx"}  # indexed before the text/html/email parsers


def rank_metrics(paths: list[str], want: str | list[str]) -> dict:
    """File-level ranking for one question. paths: the chunks a stage returned, best first.
    hit1: the top chunk is from an expected file. in_context: one of them is among the chunks
    (what the chat model gets to read). rr: 1/rank of the first expected file among the
    distinct files, 0 if absent."""
    wants = {w.lower() for w in ([want] if isinstance(want, str) else want)}
    files = list(dict.fromkeys(Path(p).name.lower() for p in paths))
    rank = next((i for i, f in enumerate(files, 1) if f in wants), None)
    return {"hit1": rank == 1, "in_context": rank is not None, "rr": 1 / rank if rank else 0.0}


def summarize_retrieval(rows: list[dict]) -> dict:
    """rows: {"item": {...}, "<stage>": rank_metrics(...)}. Overall per stage, then hybrid by
    question kind, and hybrid on the file types that were always indexed."""
    def agg(subset, stage):
        n = len(subset)
        return {
            "n": n,
            **{k: round(sum(r[stage][k] for r in subset) / n, 3) if n else None for k in ("hit1", "in_context", "rr")},
        }

    out = {stage: agg(rows, stage) for stage in STAGES if all(stage in r for r in rows)}
    for kind in sorted({r["item"].get("kind", "-") for r in rows}):
        out[f"hybrid/{kind}"] = agg([r for r in rows if r["item"].get("kind", "-") == kind], "hybrid")
    out["hybrid/pdf+office"] = agg(
        [r for r in rows if Path(_first_file(r["item"])).suffix.lower() in ORIGINAL_TYPES], "hybrid"
    )
    return out


def _first_file(item: dict) -> str:
    f = item["file"]
    return f if isinstance(f, str) else f[0]


def run_retrieval(args, tmp: Path) -> int:
    from app.config import settings
    from app.core import indexer
    from app.core.llm.client import server_status
    from app.core.search import fts_search, reranker, vector_search
    from app.core.search.hybrid import hybrid_search
    from app.db import sqlite_fts, vector_store

    status = server_status()
    if not status["connected"] or settings.embedding_model not in status["models"]:
        print(f"Needs OpenVINO Model Server running and serving {settings.embedding_model}.")
        return 2
    if args.folder:
        folder = Path(args.folder)
    else:
        from retrieval_corpus import write

        folder = write(tmp / "corpus")
    items = [json.loads(line) for line in Path(args.questions or HERE / "retrieval_questions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    t0 = time.perf_counter()
    counts = indexer.index_folder(folder, settings.db_path, settings.vector_db_dir)
    print(f"Indexed {counts['indexed']} files in {time.perf_counter() - t0:.1f}s (semantic: {counts['semantic']})")
    if counts["keyword_only"]:
        # Scoring a half-embedded index would look like a real (worse) result.
        print(f"{counts['keyword_only']} files got no vectors (low memory or the embedder failed). Free some RAM and run again.")
        return 2

    conn = sqlite_fts.connect(settings.db_path)
    table = vector_store.open_table(vector_store.connect(settings.vector_db_dir))
    stages = {
        "keyword": lambda q: fts_search.search(conn, q, limit=args.top_k),
        "semantic": lambda q: vector_search.search(table, q, limit=args.top_k) if table is not None else [],
        "hybrid": lambda q: hybrid_search(q, limit=args.top_k)["results"],
    }
    rows = []
    for item in items:
        row = {"item": item}
        for name, fn in stages.items():
            t = time.perf_counter()
            found = fn(item["q"])
            row[f"{name}_ms"] = round((time.perf_counter() - t) * 1000)
            row[name] = rank_metrics([c["path"] for c in found], item["file"])
        rows.append(row)
        print(f"{'HIT ' if row['hybrid']['hit1'] else 'top6' if row['hybrid']['in_context'] else 'MISS'}  {item['q']}")
    conn.close()

    summary = summarize_retrieval(rows)
    median_ms = {s: _median([r[f"{s}_ms"] for r in rows]) for s in STAGES}
    rerank_model = settings.rerank_model if reranker.available() else ""
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    label = f"-{args.label}" if args.label else ""
    out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}-retrieval{label}.json"
    meta = {"embedding_model": settings.embedding_model, "rerank_model": rerank_model, "median_ms": median_ms}
    out.write_text(json.dumps({"summary": summary, **meta, "rows": rows}, indent=2), encoding="utf-8")
    print(f"\nre-ranker: {rerank_model or 'off'}   median ms per search: {median_ms}")
    print(f"{'':22} {'n':>3} {'hit@1':>6} {'top-' + str(args.top_k):>6} {'MRR':>6}")
    for name, m in summary.items():
        print(f"{name:22} {m['n']:>3} {m['hit1']!s:>6} {m['in_context']!s:>6} {m['rr']!s:>6}")
    print(f"\nSaved {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", default=None, help="default: test_corpus, or a generated corpus with --retrieval")
    ap.add_argument("--questions", default=None)
    ap.add_argument("--model", default=None, help="chat model (default: the one picked in Settings)")
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--retrieval", action="store_true", help="search only: rank the expected file, no chat model")
    ap.add_argument("--label", default="", help="added to the results file name, e.g. baseline")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="lfa-eval-"))
    os.environ.update({"DATA_DIR": str(tmp), "DB_PATH": str(tmp / "index.db"), "VECTOR_DB_DIR": str(tmp / "lancedb"), "ASSISTANT_DB_PATH": str(tmp / "assistant.db"), "API_TOKEN": "eval"})
    sys.path.insert(0, str(BACKEND))
    if args.retrieval:
        return run_retrieval(args, tmp)
    args.folder = args.folder or str(BACKEND.parent / "test_corpus")
    args.questions = args.questions or str(HERE / "questions.jsonl")

    from app.config import settings
    from app.core import indexer, ram
    from app.core.llm.answerer import answer_stream
    from app.core.llm.client import current_model, server_status
    from app.core.llm.verifier import verify
    from app.core.search.hybrid import hybrid_search

    model = args.model or current_model()
    status = server_status()
    if not status["connected"]:
        print("OpenVINO Model Server isn't running. Start it (scripts\\ovms-start.ps1) and try again.")
        return 2
    if model not in status["models"]:
        print(f"OVMS doesn't serve {model}. Export it with scripts\\ovms-export.ps1.")
        return 2

    items = [json.loads(line) for line in Path(args.questions).read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"Indexing {args.folder} …")
    t0 = time.perf_counter()
    counts = indexer.index_folder(Path(args.folder), settings.db_path, settings.vector_db_dir)
    index_s = time.perf_counter() - t0
    print(f"  {counts['indexed']} files in {index_s:.1f}s (semantic search: {counts['semantic']})")

    rows = []
    min_free = None
    for n, item in enumerate(items, 1):
        chunks = hybrid_search(item["q"], limit=args.top_k)["results"]
        answer, first = "", None
        start = time.perf_counter()
        try:
            for delta in answer_stream(item["q"], chunks, model=model) if chunks else []:
                if first is None:
                    first = time.perf_counter() - start
                answer += delta
        except Exception as exc:
            answer = f"[error: {exc}]"
        total = time.perf_counter() - start
        free = ram.status()["free_mb"]
        if free is not None:
            min_free = free if min_free is None else min(min_free, free)
        verdict = verify(answer, chunks)
        if item.get("mode") == "files_only":  # the FILES ONLY gate the chat applies after verifying
            from app.api.routes_chat import files_only

            answer, verdict = files_only(answer, verdict)
        s = score(item, answer, chunks, verdict)
        rows.append({"item": item, "answer": answer, "score": s, "relevance": relevance(item["q"], answer, chunks, verdict), "first_token_s": first, "total_s": round(total, 2)})
        mark = {True: "PASS", False: "FAIL", None: "----"}[s["correct"]]
        print(f"[{n}/{len(items)}] {mark} {total:5.1f}s  {item['q']}")

    summary = {**summarize(rows), "model": model, "embedding_model": settings.embedding_model, "index_s": round(index_s, 1), "min_free_ram_mb": min_free}
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^A-Za-z0-9.-]+', '_', model)}.json"
    out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")
    print("\n" + "\n".join(f"  {k:24} {v}" for k, v in summary.items()))
    print(f"\nSaved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
