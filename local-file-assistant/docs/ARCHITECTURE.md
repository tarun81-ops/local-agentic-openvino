# Architecture

Electron shell (ui/) around a FastAPI backend (backend/) that talks to OpenVINO Model
Server on localhost. One local model plays several roles through different prompts; everything
else is plain deterministic code.

## Processes

- **Electron main** (`ui/electron/main.js`) picks a free port, generates a random API
  token, starts the backend with both, and owns the main window, the Ctrl+Shift+Space
  overlay and the tray icon. Closing the window hides it; Quit is in the tray menu.
- **Backend** (`backend/run.py`) refuses to bind to anything but loopback. Every request
  needs a loopback Host header (blocks DNS rebinding), the bearer token and, from a
  browser, an allowed Origin (`app/security.py`). `/health` is public. No access log, so
  questions never land in log files. If the backend dies, Electron restarts it (3 tries)
  on the same port and token; on quit it kills the whole process tree.
- **Logs**: `%APPDATA%\LocalFileAssistant\logs\backend.log` (backend) and
  `%APPDATA%\Local File Assistant\logs\main.log` (Electron + backend console).
- **Pages** (`ui/index.html`, `ui/overlay.html`, built by Vite) get the port and token
  through the preload bridge (`window.lfa`), never from anywhere else.

## Indexing

`core/indexer.py` walks each indexed folder, hashes files (streamed), parses them into
chunks of ~250 words with 40 words of overlap (`core/chunking.py`), and stores them in
SQLite FTS5 (`db/sqlite_fts.py`) and LanceDB (`db/vector_store.py`), embedding in batches
of 32; the embedder sees each chunk with its file name and folder in front (`embed_text`), the
stored and cited text is the chunk alone. Each chunk keeps its location: page (PDF), slide
(pptx), sheet (xlsx), part (docx, txt/md/csv, html, eml, which have no reliable pages) or image. Unchanged files are skipped; files gone from disk
are pruned. If the embedder is down, files are still keyword-indexed and retried on the
next scan. Failures are logged and listed on the Index page with the reason.

Two rules keep the two stores honest. A file's content hash is written last (`mark_complete`),
after its vectors, so anything that fails or crashes part-way is retried by the next scan; a
blank hash means "keyword-only for now" and is counted per folder on the Index page. And a
file's chunks occupy one rowid range (`file id * CHUNKS_PER_FILE + chunk number`), because FTS5
reaches rowids through an index while `WHERE path = ?` reads the whole table.

Rescans skip files whose size and modified time are unchanged without reading them. A
scan can be stopped from the Index page (nothing is pruned on a partial walk). When free RAM
is below `MIN_FREE_RAM_MB` (default 1200), files are keyword-indexed only and get their
vectors on the next scan. Scanned PDF pages with no text layer are OCR'd when RapidOCR is
installed; images get OCR text and, optionally, a caption (docs/decisions/0001-images.md).

The SQLite schema is versioned. An older index is dropped on connect, its folders are
marked unscanned and rebuilt automatically at startup, and the LanceDB vectors are dropped
with it so the two stores never disagree.

`core/watcher.py` keeps the index current: create/modify re-index, delete/move remove the
old path, all debounced and run on one worker thread.

## Search and answers

- **Search** (`core/search/`): FTS5 with stopwords dropped and terms ORed (bm25 ranks),
  plus vector search, fused with Reciprocal Rank Fusion. Keyword search always works;
  semantic search is skipped if the model server is slow or down.
- **Answerer** (`core/llm/answerer.py`) streams an answer from the top chunks, citing
  labels like `(report.pdf, page 2)`.
- **Verifier** (`core/llm/verifier.py`, no LLM call) checks each citation points at a
  retrieved location and that the claim's numbers and names appear in that source.

## Organizer

`core/llm/organizer.py` finds duplicates (same sha256) and backup/temp files itself, and
asks the model only which folder each remaining file belongs in. The plan is shown for
approval. `/organize/apply` validates every action first (inside an indexed folder, source
exists, destination free) and applies nothing if any is invalid. Moves are logged before
they happen (`undo_log.jsonl`) and can be undone per batch; deletes go to the Recycle Bin.

## Model backend

OpenVINO Model Server's OpenAI-compatible `/v3` API (`OV_BASE_URL`), on the NPU, iGPU or CPU
(docs/OPENVINO.md). Chat and embeddings use the model names `chat` and `embed`, set at export
time. The model can be changed on the Settings page. OVMS keeps models loaded; only voice models
unload after `LLM_IDLE_UNLOAD_S`.

## Measuring quality

`backend/eval/run_eval.py` indexes a folder into a throwaway index, asks the questions in
`eval/questions.jsonl` (including ones the files can't answer) and reports answer accuracy,
retrieval hit rate, citation accuracy, verifier pass rate, latency and lowest free RAM.

## The assistant (conversations, memory, tasks, actions, voice)

Everything the assistant remembers lives in `assistant.db` (conversations, memories, tasks, events,
action audit trail, feedback), separate from `index.db`: the index is disposable and rebuilt on a schema
bump, this is migrated forward (`db/migrations/NNNN_*.sql`) and never dropped. Background threads in the
backend: the reminder/briefing scheduler (every 30 s, relayed to Electron over `/events/stream` for desktop
notifications) and the memory extractor (after a chat has been quiet for 5 minutes). Context capture and
paste-back use ctypes in the backend (`context_win.py`); the clipboard stays in Electron. Actions on the
PC are proposed only from the user's own words, validated in code, and run only after approval
(`core/assistant/tools`). Voice runs locally: faster-whisper (speech to text) and Piper (text to speech).
Ranking learns a little from use: files opened for similar searches may rise at most two places
(`core/assistant/learning.py`, `open_prior_max_shift`, 0 turns it off).

`eval/assistant_eval.py` checks the rules (router, dates, memory filter) without a model; `run_eval.py`
also reports an answer-relevance heuristic.

## Personalization

Everything the user can tune lives in one place, `core/personalize.py`, stored under the single key
`personalize` in `settings.json` (data dir) and edited from Settings or `GET/PUT /personalize`. `update()`
validates every key before saving, so a bad value changes nothing, and partial updates keep the rest.

- **Profile and style.** The About me text (max 800 characters), answer style (concise, detailed, study,
  simple), reply language and "cite pages" become two bounded additions to a prompt: `with_profile` puts the
  profile in front of the question, labelled as what the user said about themselves and never to be cited as
  a file (same pattern as `with_memory`), and `style_instruction` adds one line of about 40 words to the file
  prompt or the chat system prompt. Order: screen context, profile, memory, message. Search and the query
  rewrite still use the bare message. A request can override style and language for one question; an
  indexed folder can set its own style (request > folder > saved default). A test keeps the added text under
  a fixed size, because the 2B model pays for every token.
- **Citation strictness.** `verifier_strict` raises the verifier threshold from 0.6 to 0.85.
- **Memory review.** Facts learned from chats are saved as `pending` (migration 0007) and only become
  active, and usable by recall and the prompt, when the user approves them. "Remember that ..." is an
  explicit request and is active at once. The sensitive-category filter is applied before anything is saved,
  even as pending.
- **Folder profiles.** Per indexed folder: label, answer style, file-type allowlist, exclusion patterns,
  caption and OCR-only choices. Keys are resolved and case-folded paths. One function (`indexer.is_allowed`)
  is used by the folder scan and, through `index_file`, by the watcher, so they agree; saving a profile
  rescans the folder and files the new rules exclude leave both stores through the existing `_forget` path.
- **Collections and recent files.** Saved searches (`collections` table) and `GET /files/recent` (latest
  opens, then files changed this week, indexed files only). The recent list is derived from `file_opens`,
  so wiping what is learned from use clears it; collections are the user's own data and survive the wipe.
- **Appearance.** Theme (light, dark, follow Windows), accent, text size, overlay compactness and position.
  The accent drives the `--red*` tokens. `tests/test_contrast.py` reads the real tokens in `style.css` and
  fails below 4.5:1 for small text or 3:1 for UI colours in every theme and accent. The pages apply a cached
  copy at once, then the saved one; the Electron main process reads the overlay options each time it opens.
- **Performance profiles.** Battery, balanced (today's values), plugged in and auto (from
  `psutil.sensors_battery()`, balanced when there is no battery). `personalize.effective(name)` resolves a custom
  field, then the profile, then `.env`, and is what idle unload, the RAM guard, history length, captions and
  the embedder keep-alive read. A bigger model is only ever mentioned in a note; nothing switches it.
- **Study cards.** `POST /study/generate` builds flashcards or quiz questions from one indexed file within a
  character budget (sampled evenly across long files), validates the model's JSON in code, and refuses when
  RAM is low. Nothing is stored; export is a CSV.
- **Reminders about files and the new-file nudge.** A task may point at an indexed file (validated on the
  server). The new-file nudge is a proactive kind like the briefing: it needs inbox folders chosen by the
  user, reads only names, respects quiet hours, the daily cap and the once-per-day log, and only offers to
  open Organize.
