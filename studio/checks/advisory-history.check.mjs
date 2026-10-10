import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const root = resolve(process.argv[2] || '.');
const out = join(mkdtempSync(join(tmpdir(), 'faustus-advisory-history-')), 'model.mjs');
await build({ entryPoints: [join(root, 'studio/src/screens/studio/model.ts')],
  bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
const { blankTurn, restoreFromMetadata } = await import(pathToFileURL(out).href);
const diagnostic = { advisory: true, kind: 'runtime_stop', reason: 'plan_no_progress',
  reasons: ['runtime_stop:plan_no_progress'], round: 4, requires_user_input: false,
  detail: 'El seguimiento no acredita cierre (0/3). Se conserva el trabajo; no hace falta una aprobación.',
  plan: { done: 0, total: 3, task_title: 'Create alpha.txt' } };
const harness = { tool_calls: 2, failed_calls: 1, mutations: ['alpha.txt'],
  stop_reason: 'plan_no_progress', round_count: 4, answer_diagnostics: [diagnostic] };
const source = blankTurn('assistant', 'Saved answer remains intact.');
const restored = restoreFromMetadata(source, { harness });
assert.equal(restored.checks.length, 1);
assert.equal(restored.checks[0].detail, diagnostic.detail);
assert.equal(restored.checks[0].status, 'advisory');
assert.deepEqual(restored.checks[0].reasons, diagnostic.reasons);
assert.equal(restored.text, source.text);
assert.equal(restored.ask, undefined);
assert.equal(restored.approval, undefined);
assert.equal(restored.summary.toolCalls, 2);
assert.equal(restored.summary.failedCalls, 1);
assert.equal(restored.summary.stopReason, 'plan_no_progress');
assert.deepEqual(restored.summary.mutations, ['alpha.txt']);
assert.equal(restored.rounds, 4);
assert.equal(restoreFromMetadata(restored, { harness }).checks.length, 1);

// A live notice and an unrelated real check survive without duplicates.
const live = { ...source, checks: [
  { status: 'tests_failed', detail: 'Actual failure', round: 2 },
  { status: 'advisory', detail: diagnostic.detail, round: 4, reasons: [] },
] };
const replayed = restoreFromMetadata(live, { harness });
assert.deepEqual(replayed.checks, live.checks);

// Genuine pending questions remain genuine questions, never manufactured by a notice.
const ask = { question: 'Choose a source', options: ['A', 'B'] };
assert.deepEqual(restoreFromMetadata({ ...source, ask }, { harness }).ask, ask);
assert.deepEqual(restoreFromMetadata(source, {}).checks, []);
const bad = [null, 3, [], {}, { advisory: false, detail: 'Not an advisory' }, { advisory: true }];
assert.deepEqual(restoreFromMetadata(source, { harness: { answer_diagnostics: bad } }).checks, []);
const otherNotice = { advisory: true, reasons: ['wrong_calculation'], round: 1 };
const generic = restoreFromMetadata(source, { harness: { answer_diagnostics: [otherNotice] } });
assert.deepEqual(generic.checks[0].reasons, ['wrong_calculation']);
assert.equal(generic.ask, undefined);
console.log('PASS: saved advisories survive reload, retain counters and questions, and do not duplicate live checks');

