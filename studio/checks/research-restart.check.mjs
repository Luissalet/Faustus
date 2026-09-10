// A server restart does not turn a running research into "The research failed."
//
// 10-09-2026: the follower's poll gave up on the first network error, so a
// restart of a few seconds ended the card with the last round message as
// the reason. It now rides out a short outage, and the server's
// "interrupted" answer (from the on-disk marker) reaches the screen, which
// also adopts interrupted runs from /api/research/active as failed cards.
// Run by tests/test_studio_research_restart_js.py, or by hand:
//   node studio/checks/research-restart.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-research-restart-'));
const out = join(dir, 'research.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/research.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });

// No EventSource in Node: followResearch falls back to polling. Timers run fast.
globalThis.window = { setTimeout: (fn) => setTimeout(fn, 1) };
const calls = [];
let plan = [];
globalThis.fetch = async (url, init) => {
  calls.push({ url: String(url), method: init?.method ?? 'GET' });
  const step = plan.shift();
  if (step === 'down') throw new TypeError('fetch failed');
  const [status, body] = step;
  return { ok: status < 300, status, json: async () => body, text: async () => JSON.stringify(body) };
};
const research = await import(pathToFileURL(out).href);

// 1. a short outage is ridden out; the marker's answer ends the follow
plan = ['down', 'down', 'down', [200, { status: 'running', progress: { phase: 'searching', message: 'Round 2' } }],
  'down', [200, { status: 'interrupted', progress: { phase: 'error', message: 'The server restarted while this research was running.' } }]];
const seen = [];
const status = await research.followResearch('rp-1', (p) => seen.push(p));
assert.equal(status, 'interrupted');
assert.equal(seen.at(-1).phase, 'error');
assert.match(seen.at(-1).message, /restarted/);
assert.equal(plan.length, 0, 'every planned answer was consumed');

// 2. a 404 is an answer, not an outage: the follow ends at once
plan = [[404, { detail: 'No research found for this session' }]];
assert.equal(await research.followResearch('rp-2', () => {}), 'error');
assert.equal(plan.length, 0);

// 3. a long outage still ends: the poll gives up after its retries
plan = Array.from({ length: 30 }, () => 'down');
assert.equal(await research.followResearch('rp-3', () => {}), 'error');
assert.ok(plan.length > 0 && plan.length < 30, `gave up after ${30 - plan.length} attempts`);

// 4. /api/research/active carries interrupted runs with their reason
plan = [[200, { active: [
  { session_id: 'rp-run', query: 'a', status: 'running', progress: { phase: 'searching' }, started_at: 10 },
  { session_id: 'rp-lost', query: 'b', status: 'interrupted', error: 'The server restarted while this research was running.', progress: { phase: 'error', message: 'x' }, started_at: 5, category: 'health' },
] }]];
const active = await research.activeResearch();
assert.deepEqual(active.map((a) => [a.id, a.status]), [['rp-run', 'running'], ['rp-lost', 'interrupted']]);
assert.match(active[1].error, /restarted/);
assert.equal(active[0].error, '');
assert.deepEqual(active.map((a) => a.category), ['', 'health']);

// 5. dismiss posts, and never throws when the server says no
plan = [[404, { detail: 'nope' }]];
await research.dismissResearch('rp-lost');
assert.equal(calls.at(-1).method, 'POST');
assert.match(calls.at(-1).url, /\/api\/research\/rp-lost\/dismiss$/);

console.log('ok research-restart');
