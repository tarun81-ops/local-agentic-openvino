# OpenVINO Model Server (the app's only model backend)

On the `openvino-support` branch the app runs its models with **OpenVINO Model Server (OVMS)**
on the Core Ultra's NPU, integrated GPU or CPU. Ollama is gone from this branch (it still
lives on `main`). Design and decisions: [OPENVINO-DESIGN.md](OPENVINO-DESIGN.md) and
[decisions/0002-openvino-only.md](decisions/0002-openvino-only.md).

The packaged Windows installer bundles OVMS. On first launch it creates/repairs the default model config, downloads
the chat and embedding models into `%APPDATA%\LocalFileAssistant\ovms\models`, starts OVMS in the background, and
keeps the chat screen on a setup status until `/v3` reports the selected model ready. Failed downloads and startup
are logged to `%APPDATA%\LocalFileAssistant\logs\backend.log` and `%APPDATA%\LocalFileAssistant\logs\ovms.log`
and retried automatically. No separate OVMS or model setup is needed for the installed app.

The steps below are for running the app from source (`npm run dev`) or replacing the bundled OVMS copy.

## 1. Install OVMS for development (once)

Install the Microsoft Visual C++ Redistributable (aka.ms/vs/17/release/VC_redist.x64.exe), then download
the Windows package **with Python** into your user folder (no admin rights needed; `C:\` itself is not
writable). The `python_off` package drops the system message from chat prompts, and this app relies on it.

```powershell
cd $env:LOCALAPPDATA
curl.exe -L https://github.com/openvinotoolkit/model_server/releases/download/v2026.4.0/ovms_windows_2026.4.0_python_on.zip -o ovms.zip
tar -xf ovms.zip            # creates %LOCALAPPDATA%\ovms
.\ovms\setupvars.ps1        # needed in every new PowerShell window before running ovms
ovms --version
``` Check the devices
OpenVINO sees:

```powershell
python -m pip install openvino
python -c "import openvino as ov; print(ov.Core().available_devices)"   # expect CPU, GPU, NPU
```

If NPU is missing, update the Intel NPU driver first.

## 2. Export the models (once)

```powershell
scripts\ovms-export.ps1                     # chat on the iGPU (int4), embeddings on CPU
scripts\ovms-export.ps1 -ChatDevice NPU     # or: chat on the NPU
```

The models are saved in `%APPDATA%\LocalFileAssistant\ovms\models` under the names **chat** and
**embed**, which are the app's defaults, so trying another model never needs a config change.

## 3. Start OVMS

**The backend starts OVMS by itself** (Windows): when it isn't answering and models are already configured, it runs
`%LOCALAPPDATA%\ovms\ovms.exe` hidden in the background with the flags below, and stops it when the app closes.
Its output goes to `%APPDATA%\LocalFileAssistant\logs\ovms.log`. Set `OVMS_AUTOSTART=false` in `backend\.env` to
start it yourself, or `OVMS_DIR=` if you unpacked OVMS somewhere else. To start it by hand:

```powershell
scripts\ovms-start.ps1
```

It listens on `http://127.0.0.1:8000/v3` only. The first start on NPU/GPU compiles the model
(can take a minute); later starts use the cache.

## 4. Start the app

```powershell
cd ui; npm run dev
```

`backend\.env` (copied from `.env.example` by setup):

```
OV_BASE_URL=http://127.0.0.1:8000/v3
OV_CHAT_MODEL=chat
EMBEDDING_MODEL=embed
```

After changing the embedding model, **rescan** your folders once: vectors from different
embedding models don't match.

## Good to know

- OVMS keeps models loaded. The app no longer unloads the chat model when idle; voice models
  still unload after `LLM_IDLE_UNLOAD_S`.
- Settings > Model lists what `GET /v3/models` returns. If your OVMS version doesn't serve that,
  Settings shows "not running" although answers work.
- Image captions (`CAPTION_IMAGES=true`) need a vision model exported as "chat".
- Qwen3 models may need thinking switched off for short answers; not wired up yet.
- Not benchmarked yet: compare devices with `backend\eval\run_eval.py` (plan in OPENVINO-DESIGN.md).

## Server settings that matter (from the failure-mode research, Oct 2026)

- **Embedding pooling must be LAST** for Qwen3-Embedding. OVMS defaults to CLS, and with CLS nothing errors but
  search quality drops badly. Check: open `%APPDATA%\LocalFileAssistant\ovms\models\OpenVINO\Qwen3-Embedding-0.6B-int8-ov\graph.pbtxt`
  and look for `pooling: LAST`. If it is missing, re-run the embedding `--pull` with `--pooling LAST`.
- **Long inputs are refused, not cut.** The app now cuts embedding inputs to `EMBED_MAX_CHARS` (4000) and retries an
  answer with fewer passages when OVMS says the prompt is too long.
- **Cap the memory the chat model may take** (the KV cache otherwise grows with use on 16 GB shared RAM):
  ```powershell
  & "$env:LOCALAPPDATA\ovms\setupvars.ps1"
  $models = "$env:APPDATA\LocalFileAssistant\ovms\models"
  ovms --configure --model_path "$models\OpenVINO\Qwen3-1.7B-int4-ov" --task text_generation --target_device GPU --cache_size 2 --cache_dir "$env:APPDATA\LocalFileAssistant\ovms\cache"
  ```
  Then restart OVMS. `--cache_size` is in GB; 2 is a starting point, not a measured value.
- **Sampling** is set by the app on every request (`OV_TEMPERATURE` 0.7, `OV_TOP_P` 0.8, `OV_TOP_K` 20,
  `OV_PRESENCE_PENALTY` 1.0, Qwen's non-thinking values); JSON plans use 0.2.
- **Search questions** get Qwen3-Embedding's `Instruct: ...\nQuery:` prefix (`EMBED_QUERY_INSTRUCT`); documents don't.
- **/v1 vs /v3:** OVMS 2026.3+ also serves `/v1`; `/v3` is kept as an alias for now. If a future OVMS drops it,
  set `OV_BASE_URL=http://127.0.0.1:8000/v1`.
- **NPU:** `OpenVINO/Qwen3-1.7B-int4-ov` is INT4 asymmetric (group 128) and is not NPU-compatible; an NPU model needs
  symmetric INT4, ratio 1.0, group size -1, and handles one request at a time.
