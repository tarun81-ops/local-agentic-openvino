# Local File Assistant — Issues

## Backend audit (2026-10)

Fixed in 13da45f and 9c33650: image captions and the briefing stay on this PC; a non-loopback other server needs
https://; unannotated plug-in tools named like a change ask first; MCP servers start in the background; OVMS
config.json / graph.pbtxt written atomically with device-switch rollback; keyword search filters a workspace in SQL
(also non-ASCII folder names).

| # | Status | What changed |
|---|---|---|
| A1 | FIXED | `llm_json` runs on this PC by default (`local=True`): file-agent planning and choosing and the plug-in tool planner never reach the other server; a planner whose local engine is down is skipped and says so in the step log. Answers and flashcards may use the other server; Settings, Privacy and the confirmation say "questions, flashcards and the text of the files they use". |
| A2 | FIXED | Settings > Model uses the local model name (`current_model(local=True)`) for "in use", remove, the device switch and its rollback. |
| A3 | FIXED | `PUT /models/device` answers 400 for anything but Auto, CPU, GPU or NPU (it used to become Auto). |
| A4 | FIXED | Adding, changing or removing a plug-in server returns at once; the tools are re-registered at once (a tool just marked "ask first" never runs unasked) and the servers start in the background; the page shows STARTING and polls. |
| A5 | FIXED | Vector search filters a workspace's folders and, when fewer than the wanted hits survive, looks once more from the top 2000 (same query vector). A LanceDB `where` on the path wasn't used: folder matching ignores case, also for non-ASCII names, which a SQL prefix filter can't. A workspace under ~1/250 of a big index can still miss semantic hits (keyword hits are never missed). |
| A6 | FIXED | Chat keeps only the dropped files it actually read; `tools/list` follows `nextCursor` (5 pages); server stderr goes to `logs\mcp-<server>.log` (starts over past 1 MB); each plug-in tool call is logged to `backend.log` with server, tool, argument names (never values), duration and ok/failed; workspace names are unique ignoring case (migration 0012 renames old duplicates "Name (2)"). |
| A7 | OPEN (your call) | `routes_tasks` is still wired and reachable from the UI, so it is not dead code: chat turns "remind me ..." into a reminder card (`createTask`), and REMIND ME on search rows and source cards calls `parseTask`/`createTask`; the briefing and the overdue nudge read tasks. Only the Tasks page is gone, so a saved reminder can't be listed or deleted in the app. Either remove reminders entirely (chat card, REMIND ME, scheduler toasts, routes) or keep them; nothing was deleted. |

## Earlier issues (raised 2026-09-30, worked 2026-09-30)

Status key: **FIXED** (code + tests), **READY TO VERIFY** (done here; needs your Windows laptop to confirm), **OPEN**.
Backend suite: 69 tests pass (37 before), with and without OCR installed. UI builds; checked in Chromium from file:// against a stand-in Ollama.

| # | Status | What changed |
|---|---|---|
| V1 | READY TO VERIFY | file:// origins allowed ("null" and "file://"); backend killed as a process tree on quit (venv python.exe is a launcher on Windows, so its child used to be left holding the port); backend auto-restarts up to 3 times; checklist in docs/TESTING-ON-WINDOWS.md |
| V2 | READY TO VERIFY | build-backend.ps1 bundles OCR models when installed, fails if .env / token / .db would ship, then starts the frozen exe and checks /health and the token before electron-builder runs |
| V3 | FIXED | schema upgrade keeps folders, marks them unscanned, drops stale LanceDB vectors, rebuilds automatically at startup (QUEUED/SCANNING on the Index page); tests for v1 -> v2 |
| V4 | READY TO VERIFY | live test for the /chat SSE stream added; both live tests pass against a stand-in, need the real model on the laptop |
| C1 | READY TO VERIFY | scripts\cleanup-leftovers.ps1 sends them to the Recycle Bin (run it once); pytest.ini stops the old scripts from breaking `pytest` (test_vertical_slice.py failed collection) |
| C2 | FIXED | .env ignored explicitly; build refuses to ship it |
| C3 | OPEN | root package-lock.json not inspected (laptop was unreachable); .claude\scheduled_tasks.lock belongs to Claude tooling, leave it |
| R1 | FIXED | backend\eval\run_eval.py + questions.jsonl (incl. unanswerable questions): accuracy, retrieval, citations, verifier, latency, min free RAM |
| R2 | FIXED | free-RAM guard (keyword-only indexing + chat warning when low), chat model unloaded after 10 idle min, embedder after 2 min; removed the no-op OLLAMA_KEEP_ALIVE=-1; FREE MEMORY on Settings |
| R3 | FIXED | unchanged files skipped by size+mtime without hashing; STOP button; files/min shown while scanning |
| R4 | FIXED (partly) | scanned PDF pages OCR'd (real RapidOCR checked); hybrid RRF already in place; chunk-size tuning waits for eval data |
| R5 | FIXED | refuses links, case-insensitive collisions, paths over 259 chars, names Windows rejects (also filtered from model suggestions); partial-failure + undo tested |
| R6 | FIXED | Host-header check (DNS rebinding), backend exits if HOST isn't loopback, uvicorn access log off (queries no longer logged); token already per launch |
| R7 | READY TO VERIFY | LLM_PROVIDER=openai routes chat + embeddings to any OpenAI-compatible server; docs/OPENVINO.md; not benchmarked |
| R8 | FIXED | decision recorded (docs/decisions/0001-images.md): OCR + optional VL captions (CAPTION_IMAGES), no CLIP for now |
| R9 | FIXED | GitHub Actions on Windows (pytest + UI build); scripts\lock-backend.ps1 + setup uses the lock; log files for backend and Electron |
| R10 | FIXED | small grey and red text now pass WCAG AA (4.5:1), filled buttons darker red, focus rings on dark/red, overlay results announced to screen readers (combobox/listbox), live status regions |

Found while fixing: the verifier confirms a claim is in the cited source but not that it answers the question (an off-topic answer with a real citation passes). Tracked in ROADMAP.

---


Sources: project status doc, architecture notes, and the folder listing of `local agent/local-file-assistant`.
Priority: P0 blocks a usable release, P1 should be done before daily use, P2 nice to have.

## Unverified (verify before trusting)

### V1 (P0) Run the whole app on Windows inside Electron
Only the backend (pytest) and the built UI (in Chromium against a stand-in Ollama) were tested. Electron main/preload, tray icon, close-to-tray, launch at startup, free-port-per-launch and the Ctrl+Shift+Space overlay have never been run together on Windows.
- Steps: `scripts\setup.ps1` then `scripts\start-dev.ps1`; ask a question, search, index a folder, open the overlay.
- Done when: a full ask → cited answer flow works in the Electron window, and the overlay hotkey opens and closes reliably.

### V2 (P0) PyInstaller backend and electron-builder installer
`scripts\build-backend.ps1` and the `extraResources` packaging are untested.
- Check: frozen backend starts, picks a free port, passes /health, finds bundled fonts and the RapidOCR models, and works on a clean Windows user account with no Python installed.
- Done when: the installer installs, launches and answers a question on a machine without dev tools.

### V3 (P1) SQLite schema v2 migration
Schema version bumped to 2; the existing index is rebuilt on first scan.
- Check: a v1 `index.db` upgrades cleanly, the rebuild shows progress on the Index page, and the LanceDB vectors stay in sync with the SQLite rows afterwards.
- Add a pytest for the v1 → v2 path.

### V4 (P1) Live Ollama test with the real model
`test_live_ollama.py` exists but the real qwen3-vl:2b-instruct has not been exercised end to end through /chat SSE, verifier and organizer prompts.
- Done when: the live test passes on the target laptop and the results (latency, RAM) are recorded.

## Cleanup

### C1 (P2) Delete leftover unused files
`ui/src/api/search.js`, `ui/src/pages/index-settings.js`, `ui/electron/overlay.html`, `backend/test_organizer.py`, `backend/test_vertical_slice.py`, `backend/app/core/llm/image_tagger.py`, `backend/data/` (old scratch incl. `smoke_test.db`, `organize_scratch/`), `local-file-assistant/data/` (old index location).
- Move to Recycle Bin, not permanent delete. Confirm nothing imports them first (`grep`).

### C2 (P0) Make sure `backend/.env` is never committed or packaged
`backend/.env` exists next to `.env.example` and holds the API token. Confirm `.gitignore` covers it and that PyInstaller / electron-builder do not bundle it.

### C3 (P2) Remove stray files from the folder root
`.claude/scheduled_tasks.lock` and the top-level `package-lock.json` (no matching package.json seen) look accidental; keep test corpus generated files out of source control if they are regenerated by `generate.py`.

## Risks and improvements

### R1 (P1) Answer quality of a 2B model
qwen3-vl:2b was chosen because 4B left ~0.35 GB free. Build a small evaluation set (30–50 questions over `test_corpus` plus real docs) measuring answer correctness, citation accuracy and verifier catch rate, so model or prompt changes can be compared.

### R2 (P1) RAM headroom on 16 GB shared-memory hardware
Ollama model + backend + Electron + embeddings + optional OCR all share RAM. Add a memory budget check (warn or degrade when free RAM is low), unload the model after idle time, and cap parallel indexing workers.

### R3 (P1) Indexing speed and first-scan experience
Embeddings are batched at 32 on CPU. Measure files/minute on a typical folder, add pause/resume and cancel on the Index page, and skip unchanged files by hash/mtime after the v2 rebuild.

### R4 (P1) Retrieval quality
FTS query drops stopwords and ORs terms. Evaluate hybrid ranking (BM25 + vector, e.g. reciprocal rank fusion), chunk size (~250 words / 40 overlap) and reranking; handle scanned PDFs where text extraction returns nothing (fall back to OCR).

### R5 (P1) Organizer safety review
Apply validates paths inside indexed folders, never overwrites, logs before moving, and has undo. Add tests for: symlinks/junctions escaping the folder, case-insensitive Windows name collisions, long paths (>260), files locked by another app, and undo after a partial batch failure.

### R6 (P1) Security hardening
Token is required and Origin is checked. Add tests for DNS-rebinding style Host headers, ensure the backend never binds beyond 127.0.0.1, redact file contents and tokens from logs, and rotate the token per install.

### R7 (P2) NPU / OpenVINO path
Ollama is used through an OpenAI-compatible API so the backend can be swapped later. Prototype an OpenVINO backend on the Core Ultra NPU/iGPU and compare speed and RAM with CPU Ollama.

### R8 (P2) Image search (CLIP) and image tagging
`image_tagger.py` is being removed; decide whether image understanding comes from the VL model, CLIP embeddings, or OCR only, and document it.

### R9 (P2) CI and packaging hygiene
Add a GitHub Actions (Windows) job running pytest and `npm run build`; pin dependency versions with a lockfile for the backend; add crash logging to a file in the app data folder for the installed build.

### R10 (P2) Accessibility and UX polish for the Modernist UI
Keyboard navigation, focus states, contrast of Space Mono on the chosen palette, and empty/error states for Ask, Search, Organize and Index pages.
