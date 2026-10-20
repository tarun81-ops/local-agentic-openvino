# ADR-0002: OpenVINO Model Server as the only model backend (openvino-support branch)

**Status:** Proposed
**Date:** 2026-10-18
**Deciders:** Tarun Verma

## Context
The app ran its chat model and embedder through Ollama on the CPU, with an untested switch for OpenAI-compatible servers. The laptop (Core Ultra 5 225H) also has an Arc iGPU and an NPU that Ollama can't use. Tarun decided that the `openvino-support` branch runs on OpenVINO only, with Ollama code removed; `main` keeps the Ollama build.

## Decision
Use **OpenVINO Model Server (OVMS)** as a separate local process, reached through its OpenAI-compatible `/v3` API on 127.0.0.1. Models are exported under the fixed names `chat` and `embed`. Ollama code is removed from this branch.

## Options considered

### Option A: OVMS sidecar (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low in the backend (existing OpenAI client); medium for supervising a second process |
| Cost | Free; one extra download plus model export |
| Scalability | Enough for one user; continuous batching on CPU/GPU |
| Familiarity | The client code and API shape are already in use |

**Pros:** process isolation (an NPU driver crash can't kill the backend); almost no new inference code; the same API on Windows and Linux.
**Cons:** separate install and export step; models stay loaded (RAM); one more process to start and stop.

### Option B: `openvino_genai` inside the backend
| Dimension | Assessment |
|---|---|
| Complexity | Medium–high: own streaming, cancelling, locking and embeddings code |
| Cost | Free; PyInstaller bundle grows by hundreds of MB |
| Scalability | One process, manual serialization |
| Familiarity | New API |

**Pros:** `pip install` only, no server; full control of devices and unloading.
**Cons:** crashes and memory are shared with the backend; more code to write and test.

### Option C: keep Ollama next to OVMS
Rejected for this branch at Tarun's request; it remains possible by merging with `main` later behind a shared interface.

## Trade-off analysis
A gives the NPU/iGPU with the least new code and keeps the backend safe from driver crashes; its cost is setup friction and resident RAM. B removes the friction but moves risk into the backend process. For a hackathon timeline with one developer, A is cheaper to get working and to debug. B stays the fallback if setup friction proves to be the main problem.

## Consequences
- Easier: one code path; model experiments are a re-export; NPU and iGPU available.
- Harder: first-time setup (OVMS install plus export); RAM is not freed when idle; keeping `main` and this branch in sync.
- Revisit: after the benchmark (device default), after the first outside user tries setup (bundling OVMS), if idle RAM becomes a problem (stop-when-idle).

## Action items
1. [ ] Run the step-1 verification on the laptop (OPENVINO-DESIGN.md §6)
2. [ ] OVMS supervisor in Electron
3. [ ] Hardening in `client.py`: semaphore, JSON fallback, timing logs, embedder fingerprint
4. [ ] Benchmark and pick the default device
5. [ ] Installer decision: bundle OVMS or download on first run
