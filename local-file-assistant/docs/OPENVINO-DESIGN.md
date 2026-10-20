# OpenVINO-only architecture — Local File Assistant (`openvino-support` branch)

Status: proposed, 2026-10-18. Replaces the earlier "Ollama and OpenVINO side by side" version of this doc.
Decision record: [decisions/0002-openvino-only.md](decisions/0002-openvino-only.md). Setup steps: [OPENVINO.md](OPENVINO.md).
All commands are for Windows 11 / PowerShell.

## 0. What already changed on this branch (step 0, done)

Ollama-only code is removed; the OpenAI-compatible path is now the only one.

| Area | Change |
|---|---|
| `config.py` | `OLLAMA_URL`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `LLM_PROVIDER`, `EMBED_KEEP_ALIVE` removed. New: `OV_BASE_URL` (default `http://127.0.0.1:8000/v3`), `OV_CHAT_MODEL=chat`, `EMBEDDING_MODEL=embed` |
| `core/llm/client.py` | Talks to OVMS only. `ollama_status()` became `server_status()` (`GET /v3/models`). The picked model is saved as `ov_model`, so an old Ollama name can't be sent to OVMS |
| `core/llm/idle.py` | Unloading through Ollama's `keep_alive` removed. Keeps `touch()` / `recently_used()` so background work still backs off while you chat |
| `core/search/vector_search.py` | Native `/api/embed` removed; only `/v3/embeddings` |
| `api/routes_settings.py`, UI (`main.js`, `chat.js`, `settings.js`, `sidebar.js`) | `ollama` field became `server`; texts say OpenVINO; the "embedder idle" setting removed |
| `personalize.py` | Profiles no longer set `embed_keep_alive`; "unload after" now applies to voice models only |
| Eval / diagnose | They check OVMS and its model names instead of `ollama pull` |
| Tests | Idle-unload tests removed; `test_live_ollama.py` renamed to `test_live_ovms.py`; fixtures renamed |
| Scripts | New `scripts\ovms-export.ps1` and `scripts\ovms-start.ps1`; setup scripts and docker-compose point at OVMS |

Ollama is still available on `main`; nothing there was touched.

## 1. Requirements

**Functional**
- F1. Chat, verify, organize-JSON and (optional) image captions run on a model served by OVMS.
- F2. Embeddings for indexing, memory recall and chat recall come from OVMS.
- F3. The user can see which model and device answered, and whether the server is running.
- F4. Swapping the model or device needs no code or config change (stable names `chat` and `embed`).

**Non-functional**
- One user, one interactive request at a time; background indexing must not delay a chat answer.
- 16 GB RAM shared by CPU, iGPU and NPU: OpenVINO does not free RAM. What it gains is a free CPU and lower power, if the benchmark confirms it.
- Privacy as before: file text never leaves the PC; OVMS listens on 127.0.0.1 only.
- Setup a judge or a new user can follow in under 15 minutes, once models are exported.

**Constraints:** Core Ultra 5 225H, Windows first (Linux later), one developer, hackathon timeline.

## 2. Classifying the component

This is an **outbound integration to a local, long-running daemon** (OVMS), plus the client layer in the backend that uses it. It is not a public API: the only caller is the app's own FastAPI backend, and the only caller of that is the Electron UI. The failure model is "a sidecar on the same machine": it may be missing, still starting, crashed or slow, but it is never remote.

## 3. High-level design

```
 Electron main (ui/electron/main.js)
   ├─ spawns + supervises ─► FastAPI backend  127.0.0.1:<free port>  (token, Origin, Host checks)
   │                            │
   │                            ├─ InferenceClient (core/llm/client.py + vector_search.embed)
   │                            │     chat_stream · llm_json · embed · server_status
   │                            │           │  HTTP, OpenAI-compatible
   │                            │           ▼
   └─ (phase 2) supervises ─► OpenVINO Model Server  127.0.0.1:8000/v3
                                  ├─ "chat"  → LLM / VLM pipeline → NPU | GPU | CPU
                                  └─ "embed" → embeddings pipeline → CPU | GPU
                                  model repo: %APPDATA%\LocalFileAssistant\ovms\models
                                  compile cache: %APPDATA%\LocalFileAssistant\ovms\cache
```

**Why a stable alias:** `ovms-export.ps1` exports any Hugging Face model with `--model_name chat` / `--model_name embed`. The app always asks for `chat` and `embed`, so moving from Qwen2.5-3B on the iGPU to another model on the NPU is a re-export plus an OVMS restart, with no app change.

## 4. The six design steps

### 4.1 Context
Done above: a local sidecar plus its client. The caller is our backend, which never retries on its own (`max_retries=0`); the UI shows errors to the user, who decides whether to ask again.

### 4.2 Data model

| Entity | Where | Invariant |
|---|---|---|
| Model repository + `config.json` | `%APPDATA%\LocalFileAssistant\ovms\models` | Holds exactly one `chat` and one `embed` entry; written only by `ovms-export.ps1` |
| Compile cache | `...\ovms\cache` | Disposable; deleting it only makes the next start slower |
| Selected chat model | prefs `ov_model` (falls back to `OV_CHAT_MODEL`) | Must be a name OVMS serves; `PUT /settings/model` refuses unknown names when OVMS is up |
| Vectors (LanceDB, `memories.embedding`, `chat_index.embedding`) | data dir | All vectors in one table come from one embedder. **Gap today:** the name is always `embed`, so `embed_model` can't tell two exported models apart. Fix in step 3: store a fingerprint `source_model:dim` from `ovms-export.ps1` (written to `models\embed.json`) and compare it on startup |

Model lifecycle as the app sees it: `not installed → exported → starting (compiling) → ready → failed`. The app can only observe `ready` vs not reachable today; phase 2 adds `starting`.

### 4.3 Failure modes

| # | Failure | What the user sees today | Planned handling |
|---|---|---|---|
| 1 | OVMS not installed / not started | Sidebar "OpenVINO — not running"; chat errors | Phase 2: Electron starts OVMS; Settings has the ovms.exe path and a Start button |
| 2 | Port 8000 in use | OVMS fails to start | Start script checks the port; supervisor picks another and passes it to the backend as `OV_BASE_URL` |
| 3 | `chat` or `embed` missing from config | `/models` lacks it; `diagnose.py` says which | Same, plus Settings names the missing model |
| 4 | First start compiles for a minute | First answer looks frozen or times out | Warm-up request at start; status "warming up"; the 300 s timeout already covers it |
| 5 | NPU driver missing or crashes | OVMS errors or exits | Supervisor restarts OVMS with backoff; after 3 crashes it suggests re-exporting for GPU |
| 6 | Prompt longer than an NPU-compiled model accepts | Generation error | Keep `history_token_budget` + chunk count under the export's max prompt length; show "question too long for the NPU model" |
| 7 | `response_format=json_object` ignored | `llm_json` returns `{}` (organizer gets nothing) | Prompt-only JSON + tolerant parse + one retry |
| 8 | `/v3/models` not served by this OVMS version | Settings says "not running" although answers work | Fall back to a 1-token chat call as the health probe |
| 9 | Embedding model changed | Search quietly returns poor matches | Fingerprint check (4.2) → banner "rescan needed", keyword search until done |
| 10 | Low free RAM | Already handled: keyword-only indexing, chat warning | Unchanged |
| 11 | STOP pressed mid-answer | Stream closes; OVMS may keep generating | Verify OVMS cancels on disconnect; if not, cap `max_tokens` lower |
| 12 | OVMS hangs (no tokens) | Waits up to `LLM_TIMEOUT_S` | Unchanged timeout; supervisor kills OVMS when the health probe fails twice |

### 4.4 Authorization
- Principal: the local Windows user. The backend's existing guards stay: per-launch token, Origin allowlist, Host-header check, loopback-only bind.
- OVMS has no authentication. Mitigation: `--rest_bind_address 127.0.0.1` (in `ovms-start.ps1`). Accepted risk: any program running as the user can call OVMS, the same exposure Ollama had. OVMS never sees the API token and never touches files; it only receives prompt text.
- docker-compose is the one place that needs a non-loopback bind; the file says so.

### 4.5 Idempotence and concurrency
- Chat and embedding calls are read-only; replaying one is safe. The backend still doesn't retry automatically, because a retry doubles the wait.
- **Concurrency:** one generation at a time on the NPU. Add a process-wide semaphore in `client.py` around chat calls, and have indexing embeddings wait while `idle.recently_used(30)` (already used by background workers). The chat model and the embedder run on different devices by default (GPU/NPU vs CPU), so they don't compete.
- `ovms-export.ps1` is re-runnable (`--overwrite_models`); `ovms-start.ps1` refuses to start without a config.

### 4.6 Observability
- Per request (no content): model, device, time to first token, tokens/s, prompt tokens, free RAM before and after → `backend.log`.
- `diagnose.py` section 2 checks OVMS and both model names (done).
- Settings: Connection, model list, and (step 3) "last answer: N tok/s · device · X s to first token".
- `eval\run_eval.py` stores the same numbers, so device and model runs can be compared.

## 5. Trade-offs of going OpenVINO-only

| Lost vs the Ollama build | Gained |
|---|---|
| One-command `ollama pull`; now a Python export step (~10 min per model the first time) | NPU and iGPU usable; CPU free for OCR and indexing |
| RAM given back when idle (Ollama unloaded after 10 min); OVMS keeps models loaded | One code path: fewer branches, fewer tests, simpler Settings |
| qwen3-vl captions: may not be supported by your OVMS version, so captions may stay off until a supported VLM is exported | Compile cache plus a long-running server: steady latency after warm-up |
| The two builds diverge: fixes on `main` must be merged by hand | Stable model aliases make model experiments cheap |

The RAM point matters most on 16 GB. If idle RAM turns out to be a problem, the supervisor (phase 2) can stop OVMS after N idle minutes and start it again on the next question, at the cost of a warm start.

## 6. Build sequence

1. **Verify on the laptop (no new code).** `scripts\setup.ps1`, `scripts\ovms-export.ps1`, `scripts\ovms-start.ps1`, then `pytest`, `pytest tests\test_live_ovms.py -s`, `python eval\diagnose.py`. Fix the export flags if your OVMS version names them differently.
2. **OVMS supervisor in Electron.** Spawn `ovms.exe` (path in Settings), health probe, restart with backoff, kill the process tree on quit, free-port fallback, optional stop-when-idle. Settings: Start/Stop, "warming up" state.
3. **Inference hardening in `client.py`:** chat semaphore, JSON fallback, health-probe fallback, thinking-off option, per-request timing logs, embedder fingerprint + rescan banner.
4. **Benchmark** iGPU vs NPU vs CPU, int4 vs int8, with `eval\run_eval.py`: accuracy, citation accuracy, time to first token, tokens/s, peak RAM, battery drain over 10 minutes. Pick the default device from the numbers. Suggested rule: NPU becomes the default only if accuracy stays within 3 points of the iGPU and time to first token is at most 1.5x.
5. **Installer:** decide whether to ship OVMS inside the installer (bigger download, zero setup) or download it on first run; ship pre-exported models or export on first run.
6. **Linux:** same design; OVMS release package or Docker image; NPU driver setup differs.

Skills to apply while building: error paths → error-handling-as-design; supervisor and retries → idempotency-and-side-effects; timing logs → observability-by-default.

## 7. Things to check on the laptop (not verified by me)
- The exact `export_model.py` flags in your OVMS version (`--model_name`, `--overwrite_models`, `--pooling`) and the `ovms` flags (`--rest_bind_address`, `--cache_dir`).
- Whether Qwen2.5-3B-Instruct (or the model you pick) is supported on the NPU in your OVMS version.
- Whether OVMS honours `response_format`, serves `/v3/models`, and stops generating when the client disconnects.
- Whether granite-embedding-278m exported with CLS pooling gives the same quality as before (run the retrieval eval).

## 8. What to revisit as it grows
- If setup friction blocks users: an in-process `openvino_genai` backend (one pip install, no server), at the cost of crash isolation (see ADR).
- If RAM is tight: stop-when-idle in the supervisor, or a smaller chat model.
- If `main` and this branch keep diverging: put a thin `InferenceClient` interface in front, so `main` can take this branch's code with Ollama as a second implementation.
