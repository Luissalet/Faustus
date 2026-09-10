// Resume an interrupted research from its last confirmed checkpoint.
//
// Lote 8 (TASK-02/QA-10): /api/research/active now carries a `checkpoint`
// summary on an interrupted run that has one, and POST /api/research/{id}
// /resume starts a new run from it. This drives the adapter
// (studio/src/adapters/research.ts) against a scripted fetch — the same
// shape studio/checks/research-restart.check.mjs uses for the marker/
// interrupted machinery this builds on.
// Run by tests/test_research_resume.py, or by hand:
//   node studio/checks/research-resume.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-research-resume-'));
const out = join(dir, 'research.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/research.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });

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

// 1. an interrupted run WITH a checkpoint carries it, mapped to camelCase;
// one with nothing confirmed yet carries none.
plan = [[200, { active: [
  { session_id: 'rp-cp', query: 'a', status: 'interrupted', error: 'restarted',
    progress: { phase: 'error' }, started_at: 5,
    checkpoint: { rounds_done: 3, sources: 21, report_parts: 1 } },
  { session_id: 'rp-empty', query: 'b', status: 'interrupted', error: 'restarted',
    progress: { phase: 'error' }, started_at: 6 },
] }]];
const active = await research.activeResearch();
assert.deepEqual(active[0].checkpoint, { roundsDone: 3, sources: 21, reportParts: 1 });
assert.equal(active[1].checkpoint, undefined);

// 2. resume posts to /resume and maps the new session + what was kept
plan = [[200, { session_id: 'rp-new', status: 'running', query: 'a', resumed_from: 'rp-cp',
  resumed_kept: { rounds_done: 3, sources: 21, report_parts: 1 } }]];
const resumed = await research.resumeResearch('rp-cp');
assert.equal(calls.at(-1).method, 'POST');
assert.match(calls.at(-1).url, /\/api\/research\/rp-cp\/resume$/);
assert.equal(resumed.sessionId, 'rp-new');
assert.equal(resumed.resumedFrom, 'rp-cp');
assert.deepEqual(resumed.resumedKept, { roundsDone: 3, sources: 21, reportParts: 1 });

// 3. no checkpoint on the marker yet -> the server's 409 reaches the caller
plan = [[409, { detail: 'No checkpoint to resume from yet — retry from scratch instead' }]];
await assert.rejects(research.resumeResearch('rp-empty'), (err) => {
  assert.equal(err.status, 409);
  assert.match(err.message, /No checkpoint/);
  return true;
});

// 4. a finished research's result carries resumed_from/resumed_kept when set
plan = [[200, { result: 'Final [1].', sources: [], raw_findings: [],
  resumed_from: 'rp-cp', resumed_kept: { rounds_done: 3, sources: 21, report_parts: 1 } }]];
const result = await research.researchResult('rp-new');
assert.equal(result.resumedFrom, 'rp-cp');
assert.deepEqual(result.resumedKept, { roundsDone: 3, sources: 21, reportParts: 1 });

console.log('ok research-resume');
