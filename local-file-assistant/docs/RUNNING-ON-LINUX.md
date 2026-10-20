# Running on Linux (dual boot)

The same code runs on Windows and Linux. The app picks the right paths and tools for the
system it starts on. Tested on Ubuntu with Python 3.12: the backend starts, the API answers,
and 402 of the 404 backend tests pass. The other 2 also fail on Windows and are not about Linux.

## What you need (Ubuntu 24.04)

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip curl build-essential
# Node 20 or newer (Vite 8 needs it); Ubuntu's own nodejs is too old
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
# OpenVINO Model Server and the two models: see docs/OPENVINO.md
# (Linux: OVMS release package or its Docker image; export with demos/common/export_models,
#  --model_name chat and --model_name embed)
```

Ubuntu 22.04 only has Python 3.10, which is too old (onnxruntime needs 3.11+). Either upgrade to
24.04 or install 3.12 (`sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv`)
and run setup as `PYTHON=python3.12 bash scripts/setup.sh`.

## Get the code

Commit and push on Windows first, then clone on Linux:

```bash
git clone https://github.com/tarun81-ops/local-agentic.git
cd local-agentic/local-file-assistant
```

Don't copy the Windows folder over with its `backend/.venv` or `ui/node_modules`, because those hold
Windows programs. If you do copy it, delete those two folders first.

## Set up and run

```bash
bash scripts/setup.sh      # venv, Python packages, re-ranker and voice models, npm install
cd ui && npm run dev       # opens the app (it starts its own backend)
```

`bash scripts/start-dev.sh` runs only the backend, on port 8756, for API work.

## Build an installable app (optional)

```bash
cd ui && npm run build:linux   # freezes the backend, then makes release/*.AppImage and release/*.deb
```

Build the Linux app on Linux and the Windows installer (`npm run build`) on Windows. PyInstaller
can't build one from the other.

## What's different on Linux

| | Windows | Linux |
|---|---|---|
| App data (index, chats, settings) | `%APPDATA%\LocalFileAssistant` | `~/.local/share/LocalFileAssistant` |
| Delete / Tidy-up undo | Recycle Bin | Trash |
| Everyday folders | Known Folders (follows OneDrive) | `~/.config/user-dirs.dirs` (Documents, Downloads ...) |
| PC access default | every fixed drive (C:\, D:\) | your home folder plus data partitions under `/media`, `/mnt`, `/run/media`. System folders (`/usr`, `/etc`, `/proc` ...) are never read |
| "Open Notepad / Calculator / File Explorer" | notepad, calc, explorer | gnome-text-editor or gedit, gnome-calculator, nautilus (whichever is installed) |
| Overlay: "what's on screen" + paste back | yes | off (Windows-only for now). The overlay itself still works |

Each OS keeps its own index, so on Linux the app indexes your files again on first use.

## Reading your Windows files from Linux

On a dual boot, the app can search your Windows partition too:

1. Open Files once and click the Windows drive. It mounts under `/media/<you>/<drive-name>`.
2. In the app: Settings > PC access. The Windows drive shows up in the list. Turn it on.

If Windows has **Fast Startup** on, Linux mounts that drive read-only. Search and answers still
work, but Tidy-up can't move files there. To fix it, open Control Panel > Power Options > Choose
what the power buttons do and untick "Turn on fast startup".

## Known limits

- **Global hotkey** (Ctrl+Shift+Space): may not fire on a Wayland session. If it doesn't, log out,
  click the gear on the login screen and pick "Ubuntu on Xorg".
- **Tray icon:** Ubuntu shows it. Plain GNOME needs the "AppIndicator" extension.
