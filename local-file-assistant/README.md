# Local File Assistant

Ask natural-language questions about your local files (PDF, Word, PowerPoint, Excel, text,
Markdown, CSV, HTML, saved emails, and images with OCR) and get answers with exact file + page citations. Search, and tidy folders
with a plan you approve first. 100% local: nothing leaves the machine.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/ROADMAP.md](docs/ROADMAP.md),
[docs/TESTING-ON-WINDOWS.md](docs/TESTING-ON-WINDOWS.md) and [docs/OPENVINO.md](docs/OPENVINO.md).

## Setup (Windows 11, PowerShell)

```powershell
scripts\setup.ps1                  # venv + Python packages + npm packages
scripts\ovms-export.ps1            # once: models for OpenVINO Model Server (docs/OPENVINO.md)
scripts\ovms-start.ps1             # keep this window open
cd ui; npm run dev                 # opens the app; it starts the backend itself
```

Press **Ctrl+Shift+Space** anywhere to open the search/ask overlay. Closing the window keeps
the app in the tray; quit from the tray menu.

## Make it yours

Settings > ABOUT ME & ANSWERS holds a short note about you, the answer style (concise, detailed, study,
simple), the reply language and how strictly citations are checked. What the assistant learns from chats waits
for your approval on the Memory page. Each folder on the Index page has its own file types, exclusions and
answer style, searches can be saved as collections, and Settings also has the theme and accent, the
performance profile (battery, balanced, plugged in, auto) and an opt-in nudge for new files in folders such
as Downloads. All of it is stored on this PC and sent nowhere.

## Tests

```powershell
cd backend
.venv\Scripts\python.exe -m pytest              # no model server needed
.venv\Scripts\python.exe -m pytest tests\test_live_ovms.py -s     # real model, if OVMS is running
.venv\Scripts\python.exe eval\run_eval.py       # answer quality, speed and RAM on the real model
```

## Optional

- OCR for images and scanned PDFs: `.venv\Scripts\pip.exe install "rapidocr-onnxruntime>=1.4,<2"`
- Image captions from the vision model: `CAPTION_IMAGES=true` in `backend\.env` (slow on CPU)
- Pin exact package versions after a working setup: `scripts\lock-backend.ps1`

## Installer

```powershell
cd ui; npm run build     # vite build + PyInstaller backend + NSIS installer in ui\release
```
