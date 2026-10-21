// node --test tests/  — the once-a-day rule of electron/updates.js.
const test = require('node:test');
const assert = require('node:assert');
const { shouldCheck, DAY_MS } = require('../electron/updates.js');

test('checks on first run and once a day after that', () => {
  const now = 1_800_000_000_000;
  assert.equal(shouldCheck({}, now), true);
  assert.equal(shouldCheck({ last: now - DAY_MS + 60_000 }, now), false);
  assert.equal(shouldCheck({ last: now - DAY_MS }, now), true);
});

test('never checks when switched off in Settings', () => {
  assert.equal(shouldCheck({ enabled: false }, Date.now()), false);
});

test('a clock set backwards does not stop checks forever', () => {
  assert.equal(shouldCheck({ last: Date.now() + 10 * DAY_MS }, Date.now()), true);
});
