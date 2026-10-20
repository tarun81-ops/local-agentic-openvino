"""Read-only health check for whole-PC questions, run against YOUR real data.

    cd backend
    .venv\\Scripts\\python eval\\diagnose.py "what is my class 10 percentage"

Prints PASS / WARN / FAIL for each link in the chain and a list of likely causes.
It changes nothing (no scan, no indexing). It does call the local model for the question, like the app does.
Close the app first if you can; it only reads the same databases the app uses.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CAUSES: list[str] = []


def show(level: str, what: str, detail: str = "") -> None:
    print(f"[{level:4}] {what}" + (f"  -> {detail}" if detail else ""), flush=True)


def cause(text: str) -> None:
    if text not in CAUSES:
        CAUSES.append(text)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def main() -> int:
    question = " ".join(sys.argv[1:]).strip() or "what is my class 10 percentage"
    print(f"Question: {question}")

    from app.config import settings
    from app.core import pc_access, pc_content
    from app.core.assistant import file_agent, router
    from app.db import sqlite_fts

    print(f"Data folder: {settings.data_dir}")

    # 1. PC access settings
    section("1. Whole-PC access settings")
    cfg = pc_access.config()
    if cfg["enabled"]:
        show("PASS", "PC access is ON", f"drives={cfg['drives']}")
    else:
        show("FAIL", "PC access is OFF")
        cause("PC access is off. Index page -> 02 WHOLE-PC ACCESS -> turn on and pick a drive.")
    if cfg["enabled"] and not cfg["drives"]:
        show("FAIL", "No drive selected")
        cause("PC access is on but no drive is ticked.")
    show("INFO", "read_content", str(cfg.get("read_content")))
    show("INFO", "read_images", str(cfg.get("read_images")))
    if not cfg.get("read_content"):
        cause("'Read file contents' is off, so only names are searched, never what is inside.")

    # 2. OpenVINO Model Server
    section("2. Local model server (OpenVINO Model Server)")
    from app.core.llm.client import current_model, server_status

    st = server_status()
    if st["connected"]:
        show("PASS", "OVMS reachable", f"{len(st['models'])} model(s) at {settings.ov_base_url}")
        for want in (current_model(), settings.embedding_model):
            if want in st["models"]:
                show("PASS", f"model served: {want}")
            else:
                show("FAIL", f"model missing: {want}", r"run: scripts\ovms-export.ps1, then restart OVMS")
                cause(f"OVMS does not serve a model named {want}.")
    else:
        show("FAIL", "OVMS not reachable", str(st.get("error", ""))[:120])
        cause(r"OpenVINO Model Server is not running (scripts\ovms-start.ps1). Without it nothing can plan or answer.")

    # 2b. OCR
    section("2b. OCR (needed for scanned PDFs and photos)")
    from app.core.parsers import image_parser

    if image_parser.available():
        show("PASS", "OCR installed (rapidocr)")
    else:
        show("FAIL", "OCR NOT installed", "scanned marksheets/certificates can't be read")
        cause("OCR is missing, so scanned PDFs/photos give 'No readable text'. Fix: .venv\\Scripts\\pip install rapidocr-onnxruntime")

    # 3. Names catalog
    section("3. Names catalog (every file on the PC, name + path)")
    terms: list[str] = []
    try:
        st = pc_access.status()
        show("INFO", "files in catalog", str(st.get("files")))
        show("INFO", "last scan", str(st.get("last_scan")))
        show("INFO", "crawler", f"running={st.get('running')} scanned={st.get('scanned', '')}")
        if not st.get("files"):
            show("FAIL", "catalog is empty")
            cause("The names catalog is empty: the first scan hasn't finished (or never ran). Keep the app open on the Index page.")
        terms = pc_access.keywords(question)
        show("INFO", "search words", ", ".join(terms) or "(none)")
        hits = pc_access.find(terms, limit=10) if terms else []
        if hits:
            show("PASS", f"{len(hits)} name match(es)")
            for h in hits[:8]:
                print("        ", h.get("path"))
        else:
            show("WARN", "no file NAME matches those words")
            cause("No file name contains those words. If the marksheet is named e.g. 'IMG_2031.jpg' only reading contents can find it.")
    except Exception as exc:
        show("FAIL", "catalog lookup crashed", repr(exc)[:200])
        cause(f"Catalog problem: {exc!r}")

    # 4. Content index
    section("4. Content index (what the app has actually read)")
    try:
        conn = sqlite_fts.connect(settings.db_path)
        try:
            roots = [r[0] for r in conn.execute("SELECT path FROM roots WHERE hidden=0")]
            hidden = sqlite_fts.hidden_roots(conn)
            n_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            n_hidden = 0
            for hr in hidden:
                n_hidden += conn.execute("SELECT COUNT(*) FROM files WHERE root=?", (hr,)).fetchone()[0]
        finally:
            conn.close()
        show("INFO", "folders you added by hand", str(len(roots)))
        show("INFO", "drive roots (hidden)", f"{len(hidden)}: {hidden}")
        show("INFO", "files indexed total / from PC drives", f"{n_files} / {n_hidden}")
        prog = pc_content.progress()
        show("INFO", "reading progress", str(prog))
        w = pc_content.worker.state()
        show("INFO", "worker state (this process)", str(w))
        why = pc_content.pause_reason()
        show("INFO", "pause reason right now", why or "none")
        if cfg["enabled"] and cfg.get("read_content"):
            if not hidden:
                show("FAIL", "no drive roots registered")
                cause("Drive roots are missing. Toggle PC access off and on once, or restart the backend, so sync_roots runs.")
            elif n_hidden == 0:
                show("WARN", "nothing read from your PC yet")
                cause("Nothing has been read yet. The background reader only runs while the app (backend) is open and the PC is idle. "
                      "Wait, keep the app open, and plug in power. It can take hours for a whole drive.")
            else:
                pct = 100 * prog["done"] // max(1, prog["eligible"])
                show("PASS", f"{n_hidden} PC file(s) read", f"{pct}% of eligible files processed")
                if pct < 100:
                    cause(f"Only {pct}% of your files have been read so far; the marksheet may still be waiting in the queue. "
                          "Live reading (step 6) can still find it.")
    except Exception as exc:
        show("FAIL", "content index check crashed", repr(exc)[:200])
        cause(f"Content index problem: {exc!r}")

    # 5. Does the app decide to look at the PC at all?
    section("5. Does this question trigger a PC lookup?")
    worthy = file_agent.agent_worthy(question)
    if worthy:
        show("PASS", "agent_worthy = True")
    else:
        show("FAIL", "agent_worthy = False", "the app would NOT look in your files for this question")
        cause("The question doesn't look like a file/personal question to the trigger. Tell me the exact wording that fails.")

    # 6. Live agent
    section("6. Live agent (understand -> find -> choose -> read)")
    got: dict = {"chunks": [], "files": []}
    if not cfg["enabled"]:
        show("SKIP", "PC access is off, agent would not run")
    else:
        t0 = time.time()
        try:
            gen = file_agent.gather(question)
            while True:
                try:
                    ev = next(gen)
                    show("STEP", f"{time.time() - t0:5.1f}s {ev['step']}", ev.get("detail", ""))
                    for n in ev.get("files", [])[:8]:
                        print("          -", n)
                except StopIteration as stop:
                    got = stop.value or got
                    break
            show("INFO", "total time", f"{time.time() - t0:.1f}s")
            if got["chunks"]:
                show("PASS", f"read {len(got['files'])} file(s), {len(got['chunks'])} passage(s)")
                for c in got["chunks"][:3]:
                    print("        ", c["name"], "|", (c.get("snippet") or c["text"])[:160].replace("\n", " "))
            else:
                show("FAIL", "agent produced no passages")
                cause("The agent found nothing readable. Check step 3 (name match) and the 'Skipped' lines above: "
                      "scanned PDFs and photos need 'Read images' turned on and OCR available; password-protected files can't be read.")
        except Exception as exc:
            show("FAIL", "agent crashed", repr(exc)[:300])
            cause(f"File agent error: {exc!r}")

    # 7. Search + routing
    section("7. Normal search and routing")
    try:
        from app.core.search.hybrid import hybrid_search

        res = hybrid_search(question, limit=6)
        rows = res.get("results", [])
        show("INFO", f"{len(rows)} search hit(s)", f"mode={res.get('mode')}")
        for r in rows[:5]:
            print("        ", r.get("path"))
        show("INFO", "router.route('auto')", router.route("auto", question, rows))
    except Exception as exc:
        show("WARN", "search failed", repr(exc)[:200])

    section("Summary")
    if CAUSES:
        print("Likely causes:")
        for i, c in enumerate(CAUSES, 1):
            print(f" {i}. {c}")
    else:
        print("Every link passed. If the app still answers wrongly, the break is after retrieval (the model's answer or the UI).")
        print("Please send me the exact answer the app gave.")
    print("\nPlease paste this whole output back to me.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
