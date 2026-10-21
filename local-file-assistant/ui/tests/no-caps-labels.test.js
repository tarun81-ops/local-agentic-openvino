// Calm UI regression: visible labels must be sentence case, not ALL-CAPS shouting.
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const ALLOWED = new Set(['PC', 'PDF', 'RAM', 'AI', 'CSV', 'URL', 'OK', 'MCP', 'OCR', 'NPU', 'CPU', 'GPU', 'API', 'DOC', 'XLS', 'PPT', 'IMG', 'TXT', 'JPG', 'PNG', 'GET', 'POST', 'PUT', 'PATCH', 'DELETE']);
const files = (d) => readdirSync(d).flatMap((f) => { const p = join(d, f); return statSync(p).isDirectory() ? files(p) : p.endsWith('.js') ? [p] : []; });

test('no ALL-CAPS label strings in the UI source', () => {
  const bad = [];
  for (const f of files(fileURLToPath(new URL('../src', import.meta.url)))) {
    if (/api\.js$/.test(f)) continue;
    for (const m of readFileSync(f, 'utf8').matchAll(/'([A-Z][A-Z0-9 &’…:.,\/!?()+—-]{2,})'/g)) {
      const words = m[1].split(/\s+/).filter((w) => /^[A-Z]{2,}/.test(w) && !ALLOWED.has(w.replace(/[^A-Z]/g, '')));
      if (words.length >= 1 && m[1].includes(' ')) bad.push(`${f}: ${m[1]}`);
    }
  }
  assert.deepEqual(bad, []);
});
