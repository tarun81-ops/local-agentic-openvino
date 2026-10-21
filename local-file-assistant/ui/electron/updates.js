// Auto-update from GitHub Releases (electron-updater; the release repo is `build.publish` in package.json, one repo per
// build so an Ollama install never receives the OpenVINO build). At most one check a day, only in the installed app,
// only while switched on in Settings. A found update downloads in the background and is checked against the release's
// SHA-512; the app then only says "Update ready" and installs when you click Restart or quit. It never restarts itself.
const fs = require('fs');
const path = require('path');

const DAY_MS = 24 * 60 * 60 * 1000;

/** Whether to ask GitHub now: switched on (the default) and no check in the last 24 hours. A stamp in the future
 * (the clock was set back) counts as old, or checks would stop until the clock caught up. */
function shouldCheck(saved, now) {
  const age = now - (saved.last || 0);
  return saved.enabled !== false && (age >= DAY_MS || age < 0);
}

function start({ app, ipcMain, send, log }) {
  const file = path.join(app.getPath('userData'), 'updates.json');
  const load = () => {
    try {
      return JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch {
      return {};
    }
  };
  const save = (patch) => fs.writeFileSync(file, JSON.stringify({ ...load(), ...patch }));
  let ready = null; // the downloaded version, waiting for a restart
  let updater = null; // electron-updater, installed app only
  let tick = () => {};

  ipcMain.handle('updates:get', () => ({ enabled: load().enabled !== false, supported: app.isPackaged, ready }));
  ipcMain.handle('updates:set', (_e, enabled) => {
    save({ enabled: Boolean(enabled) });
    if (enabled) tick();
    return { enabled: Boolean(enabled) };
  });
  ipcMain.handle('updates:install', () => {
    if (ready && updater) setImmediate(() => updater.quitAndInstall());
    return Boolean(ready);
  });
  if (!app.isPackaged) return; // a dev run has no release to compare against

  updater = require('electron-updater').autoUpdater;
  updater.logger = { info: log, warn: log, error: log, debug: () => {} };
  updater.on('update-downloaded', (info) => {
    ready = info.version;
    send('update:ready', ready);
  });
  updater.on('error', (err) => log(`update check failed: ${err.message}`)); // offline is normal; try again tomorrow

  tick = () => {
    const now = Date.now();
    if (ready || !shouldCheck(load(), now)) return;
    save({ last: now });
    updater.checkForUpdates().catch(() => {}); // the 'error' handler logs it
  };
  tick();
  setInterval(tick, 60 * 60 * 1000); // the app runs for days in the tray; the 24-hour rule still holds
}

module.exports = { shouldCheck, start, DAY_MS };
