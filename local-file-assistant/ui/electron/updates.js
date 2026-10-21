// Auto-update from GitHub Releases (electron-updater; the release repo is `build.publish` in package.json, one repo per
// build so an Ollama install never receives the OpenVINO build). At most one check a day, only in the installed app,
// only while switched on in Settings, never before the window is up. A found update downloads in the background and is
// checked against the release's SHA-512; the app then only says "Update ready" and installs when you click Restart now.
// It never restarts itself and never installs on quit. Prereleases and drafts are never offered.
const fs = require('fs');
const path = require('path');

const DAY_MS = 24 * 60 * 60 * 1000;
const FIRST_CHECK_MS = 10 * 1000; // after the backend and window have started

/** Whether to ask GitHub now: switched on (the default) and no check in the last 24 hours, or an update downloaded
 * earlier is still waiting (Later, then quit: show the banner again on this start). A stamp in the future (the clock
 * was set back) counts as old, or checks would stop until the clock caught up. */
function shouldCheck(saved, now, version) {
  const age = now - (saved.last || 0);
  const waiting = Boolean(saved.pending) && saved.pending !== version;
  return saved.enabled !== false && (age >= DAY_MS || age < 0 || waiting);
}

/** LFA_UPDATE_URL (scripts\test-update.ps1): a folder served on this PC stands in for GitHub Releases, to test an
 * update without publishing. Only an http(s) address on 127.0.0.1 or localhost; anything else is ignored. */
function testFeed(value) {
  if (!value) return null;
  try {
    const u = new URL(value);
    const host = u.hostname.replace(/^\[|\]$/g, '');
    const loopback = host === 'localhost' || host === '::1' || /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host);
    return (u.protocol === 'http:' || u.protocol === 'https:') && loopback ? u.href : null;
  } catch {
    return null;
  }
}

/** "github.com/<owner>/<repo>/releases" from the app-update.yml electron-builder writes into the installed app, so
 * Settings can name exactly what the check reads. null when it can't be read. */
function releasesPage(yml) {
  const owner = /^owner:\s*['"]?([\w.-]+)/m.exec(yml || '');
  const repo = /^repo:\s*['"]?([\w.-]+)/m.exec(yml || '');
  return owner && repo ? `github.com/${owner[1]}/${repo[1]}/releases` : null;
}

function start({ app, ipcMain, send, log, env = process.env, updater: injected }) {
  const file = path.join(app.getPath('userData'), 'updates.json');
  const load = () => {
    try {
      return JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch {
      return {};
    }
  };
  const save = (patch) => {
    try {
      fs.writeFileSync(file, JSON.stringify({ ...load(), ...patch }));
    } catch (err) {
      log(`could not save update state: ${err.message}`);
    }
  };
  const feed = app.isPackaged ? testFeed(env.LFA_UPDATE_URL) : null;
  let source = null;
  try {
    source = app.isPackaged ? releasesPage(fs.readFileSync(path.join(process.resourcesPath, 'app-update.yml'), 'utf8')) : null;
  } catch {
    // no app-update.yml: Settings names github.com in general
  }
  let ready = null; // the downloaded version, waiting for Restart now
  let updater = null; // electron-updater, installed app only
  let tick = () => {};

  ipcMain.handle('updates:get', () => {
    const saved = load();
    return { enabled: saved.enabled !== false, supported: app.isPackaged, ready, failed: Boolean(saved.failed), source: feed || source,
      version: app.getVersion() };
  });
  ipcMain.handle('updates:set', (_e, enabled) => {
    save({ enabled: Boolean(enabled) });
    if (enabled) tick();
    return { enabled: Boolean(enabled) };
  });
  ipcMain.handle('updates:install', () => {
    if (ready && updater) setImmediate(() => updater.quitAndInstall(false, true)); // the click is the consent; start again after
    return Boolean(ready);
  });
  if (!app.isPackaged) return; // a dev run has no release to compare against

  updater = injected || require('electron-updater').autoUpdater;
  updater.autoDownload = true; // in the background, once a newer version is found
  updater.autoInstallOnAppQuit = false; // only Restart now installs it
  updater.allowPrerelease = false;
  updater.allowDowngrade = false;
  updater.logger = { info: log, warn: log, error: log, debug: () => {} };
  if (env.LFA_UPDATE_URL && !feed) log('LFA_UPDATE_URL ignored: only an address on 127.0.0.1 or localhost is allowed');
  if (feed) {
    updater.setFeedURL({ provider: 'generic', url: feed });
    log(`update test: checking ${feed} instead of GitHub`);
  }
  if (load().pending === app.getVersion()) save({ pending: null }); // installed
  updater.on('update-available', (info) => {
    save({ failed: false });
    log(`update ${info.version} found, downloading in the background`);
  });
  updater.on('update-not-available', () => save({ failed: false }));
  updater.on('update-downloaded', (info) => {
    ready = info.version;
    save({ failed: false, pending: ready });
    log(`update ${ready} downloaded; waiting for Restart now`);
    send('update:ready', ready);
  });
  updater.on('error', (err) => {
    save({ failed: true }); // Settings: "Couldn't check"; offline is normal, try again tomorrow
    log(`update check failed: ${err?.message || err}`);
  });

  tick = () => {
    const saved = load();
    if (ready || saved.enabled === false || (!feed && !shouldCheck(saved, Date.now(), app.getVersion()))) return;
    save({ last: Date.now() });
    try {
      Promise.resolve(updater.checkForUpdates()).catch(() => {}); // the 'error' handler logs it
    } catch {
      // nor a throw: a failed check must never reach startup or the tray
    }
  };
  setTimeout(tick, FIRST_CHECK_MS).unref?.();
  setInterval(tick, 60 * 60 * 1000).unref?.(); // the app runs for days in the tray; the 24-hour rule still holds
}

module.exports = { shouldCheck, testFeed, releasesPage, start, DAY_MS, FIRST_CHECK_MS };
