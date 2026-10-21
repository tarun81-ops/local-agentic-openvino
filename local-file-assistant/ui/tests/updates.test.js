// node --test tests/  — the update rules of electron/updates.js, with a stand-in for electron-updater.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { shouldCheck, testFeed, releasesPage, start, DAY_MS, FIRST_CHECK_MS } = require('../electron/updates.js');

test('checks on first run and once a day after that', () => {
  const now = 1_800_000_000_000;
  assert.equal(shouldCheck({}, now, '0.3.0'), true);
  assert.equal(shouldCheck({ last: now - DAY_MS + 60_000 }, now, '0.3.0'), false);
  assert.equal(shouldCheck({ last: now - DAY_MS }, now, '0.3.0'), true);
});

test('never checks when switched off in Settings', () => {
  assert.equal(shouldCheck({ enabled: false }, Date.now(), '0.3.0'), false);
  assert.equal(shouldCheck({ enabled: false, pending: '0.4.0' }, Date.now(), '0.3.0'), false);
});

test('a clock set backwards does not stop checks forever', () => {
  assert.equal(shouldCheck({ last: Date.now() + 10 * DAY_MS }, Date.now(), '0.3.0'), true);
});

test('an update downloaded before a quit is offered again on the next start', () => {
  const now = Date.now();
  assert.equal(shouldCheck({ last: now - 60_000, pending: '0.4.0' }, now, '0.3.0'), true);
  assert.equal(shouldCheck({ last: now - 60_000, pending: '0.4.0' }, now, '0.4.0'), false); // installed
});

test('the test feed is only ever a loopback address', () => {
  assert.equal(testFeed('http://127.0.0.1:8765'), 'http://127.0.0.1:8765/');
  assert.equal(testFeed('http://localhost:8765/'), 'http://localhost:8765/');
  for (const bad of ['', undefined, 'http://192.168.1.20:8765', 'https://github.com/x/y', 'http://127.0.0.1.evil.com/', 'file:///C:/release', 'not a url']) {
    assert.equal(testFeed(bad), null, String(bad));
  }
});

test('Settings names the releases page of the repo in app-update.yml', () => {
  const yml = 'owner: tarun81-ops\nrepo: local-agentic-openvino\nprovider: github\nupdaterCacheDirName: x\n';
  assert.equal(releasesPage(yml), 'github.com/tarun81-ops/local-agentic-openvino/releases');
  assert.equal(releasesPage('provider: generic\n'), null);
});

test('each build only looks at its own repo', () => {
  const { build } = require('../package.json');
  const openvino = build.nsis.artifactName.includes('OpenVINO');
  assert.deepEqual(build.publish.map((p) => [p.provider, p.owner, p.repo]), [['github', 'tarun81-ops', openvino ? 'local-agentic-openvino' : 'local-agentic']]);
  assert.equal(build.appId, 'com.local-file-assistant.app'); // changing it would orphan existing installs and their data
  assert.equal(build.productName, 'Local File Assistant');
  assert.equal(build.nsis.deleteAppDataOnUninstall, false);
});

/** start() with a fake app, IPC and updater; returns what it did. */
function run(t, { saved = {}, env = {}, check } = {}) {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lfa-updates-'));
  fs.writeFileSync(path.join(dir, 'updates.json'), JSON.stringify(saved));
  const handlers = {}, on = {}, sent = [], lines = [], did = { checks: 0, installs: 0, feeds: [] };
  const updater = {
    on: (name, fn) => (on[name] = fn),
    checkForUpdates: () => {
      did.checks += 1;
      return check ? check() : Promise.resolve();
    },
    quitAndInstall: () => (did.installs += 1),
    setFeedURL: (cfg) => did.feeds.push(cfg),
  };
  start({
    app: { isPackaged: true, getPath: () => dir, getVersion: () => '0.3.0' },
    ipcMain: { handle: (name, fn) => (handlers[name] = fn) },
    send: (ch, v) => sent.push([ch, v]),
    log: (line) => lines.push(line),
    env,
    updater,
  });
  return { on, sent, lines, did, updater, ipc: (name, ...a) => handlers[name]({}, ...a) };
}

test('the check waits for startup, then a found update downloads and only offers a restart', async (t) => {
  const r = run(t);
  assert.equal(r.did.checks, 0); // nothing at startup itself
  t.mock.timers.tick(FIRST_CHECK_MS);
  assert.equal(r.did.checks, 1);
  assert.equal(r.updater.autoDownload, true);
  assert.equal(r.updater.autoInstallOnAppQuit, false); // quitting never installs it
  assert.equal(r.updater.allowPrerelease, false);
  r.on['update-downloaded']({ version: '0.4.0' });
  assert.deepEqual(r.sent, [['update:ready', '0.4.0']]);
  assert.equal(r.did.installs, 0); // no restart, no install by itself
  t.mock.timers.tick(5 * 60 * 60 * 1000);
  assert.equal(r.did.checks, 1); // waiting for the click: no more checks
  t.mock.timers.reset();
  assert.equal(r.ipc('updates:install'), true);
  await new Promise((done) => setImmediate(done));
  assert.equal(r.did.installs, 1);
});

test('switched off, nothing contacts GitHub, whatever happens', (t) => {
  const r = run(t, { saved: { enabled: false }, env: { LFA_UPDATE_URL: 'http://127.0.0.1:8765' } });
  t.mock.timers.tick(3 * DAY_MS);
  r.ipc('updates:set', false);
  assert.equal(r.did.checks, 0);
  assert.equal(r.ipc('updates:get').enabled, false);
});

test('a failed check is a log line and "couldn\'t check", never an error', async (t) => {
  const r = run(t, { check: () => { throw new Error('getaddrinfo ENOTFOUND github.com'); } });
  t.mock.timers.tick(FIRST_CHECK_MS); // the throw must not escape the timer
  await Promise.resolve();
  r.on.error(new Error('getaddrinfo ENOTFOUND github.com'));
  assert.ok(r.lines.some((l) => l.includes('update check failed: getaddrinfo ENOTFOUND')));
  assert.equal(r.ipc('updates:get').failed, true);
  r.on['update-not-available']({});
  assert.equal(r.ipc('updates:get').failed, false);
});

test('LFA_UPDATE_URL is honoured only on loopback', (t) => {
  const far = run(t, { env: { LFA_UPDATE_URL: 'http://192.168.1.20:8765' } });
  assert.deepEqual(far.did.feeds, []);
  assert.ok(far.lines.some((l) => l.includes('LFA_UPDATE_URL ignored')));
  t.mock.timers.reset();
  const near = run(t, { saved: { last: Date.now() }, env: { LFA_UPDATE_URL: 'http://127.0.0.1:8765' } });
  assert.deepEqual(near.did.feeds, [{ provider: 'generic', url: 'http://127.0.0.1:8765/' }]);
  t.mock.timers.tick(FIRST_CHECK_MS);
  assert.equal(near.did.checks, 1); // a test run checks at once, not tomorrow
  assert.equal(near.ipc('updates:get').source, 'http://127.0.0.1:8765/');
});
