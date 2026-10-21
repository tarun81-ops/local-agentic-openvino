# Releasing (OpenVINO build)

This is the `openvino-support` build. Its installers are published to
**github.com/tarun81-ops/local-agentic-openvino**; the Ollama build publishes to `local-agentic`. Each installed
app only ever looks at its own repo (`build.publish` in `ui\package.json`), so an Ollama install never updates to
the OpenVINO build or the other way round. Never publish one build into the other's repo.

## 1. Build

On the Windows PC that builds releases, once: OVMS unpacked in `%LOCALAPPDATA%\ovms` (docs/OPENVINO.md), or
`$env:OVMS_DIR` pointing at it, and `scripts\setup.ps1` run.

```powershell
cd ui
# bump "version" in ui\package.json first (0.2.0 -> 0.3.0): the updater compares versions
npm test           # the update rules (electron\updates.js)
npm run build      # web UI, frozen backend + smoke test, OVMS copied in, NSIS installer
```

`ui\release\` then holds `LocalFileAssistant-OpenVINO-Setup-<version>.exe`, its `.blockmap`, and `latest.yml`
(the version and SHA-512 the installed apps check before installing). OVMS is inside the installer; the models
are not, the setup wizard downloads them.

## 2. Sign (only if you have a code-signing certificate)

Unsigned installers work, but Windows SmartScreen warns on the first runs. With a certificate (`.pfx`):

```powershell
$env:CSC_LINK = "C:\path\to\certificate.pfx"
$env:CSC_KEY_PASSWORD = "<its password>"   # type it in this window only; never put it in a file in the repo
npm run build
```

electron-builder signs the app and the installer. Check: right-click the `.exe` > Properties > Digital Signatures.

## 3. Publish

Needs a GitHub token with `repo` scope for `local-agentic-openvino`, set only in this PowerShell window:

```powershell
$env:GH_TOKEN = "<token>"
npx electron-builder --win nsis --publish always
```

This uploads the installer, `.blockmap` and `latest.yml` to a **draft** release named after the version (draft is
electron-builder's default for GitHub). Open the repo's Releases page, paste the notes from
`docs\releases\<version>.md`, and click Publish. Installed apps see it at their next daily check; drafts and
prereleases are never offered to them.

The three files, if you prefer to upload them by hand to a draft release instead (no token in the shell):
`ui\release\LocalFileAssistant-OpenVINO-Setup-<version>.exe`, `ui\release\LocalFileAssistant-OpenVINO-Setup-<version>.exe.blockmap` and
`ui\release\latest.yml`. Upload all three: an installed app reads `latest.yml` first and refuses an installer
whose SHA-512 doesn't match it.

To test an update before users get it, follow "Update from the previous version" in docs/TESTING-ON-WINDOWS.md:
`scripts\test-update.ps1` serves `ui\release\` on 127.0.0.1 to an installed older build, so nothing is published.

What an update never touches: the index, `assistant.db`, `settings.json`, workspaces and downloaded models live in
`%APPDATA%\LocalFileAssistant`, Electron's own files in `%APPDATA%\Local File Assistant`; the installer only
replaces the program folder (`%LOCALAPPDATA%\Programs\Local File Assistant`), and the uninstaller keeps both data
folders (`deleteAppDataOnUninstall: false`). Before a database migration runs, `assistant.db` is copied to
`assistant.db.bak-<old schema version>` (the newest two are kept). Never change `appId` or `productName` in
`ui\package.json`: they decide those folders, and changing them orphans every existing install.
