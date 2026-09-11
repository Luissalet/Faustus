// Lote 71 — PLAN-01: a long plan's steps card collapses to a short preview
// (first N steps + a counter) instead of dumping every step at once once
// expanded. Proves the pure helper `planStepsPreview` (Transcript.tsx)
// through esbuild, same pattern as l65-transcript-helpers.check.mjs.
//
// Run by tests/test_l71_plan_leases.py, or by hand:
//   node studio/checks/l71-plan-steps-preview.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-l71-plan-steps-')), 'transcript.mjs');
await build({
  entryPoints: [join(root, 'studio/src/screens/studio/Transcript.tsx')], bundle: true,
  platform: 'node', format: 'esm', jsx: 'automatic', outfile: out, logLevel: 'silent',
});
// Same minimal DOM stand-ins as l65-transcript-helpers.check.mjs — module
// load touches `document`/`window.localStorage` once, nothing under test
// here calls either.
globalThis.window ??= { localStorage: { getItem: () => null, setItem: () => {} } };
globalThis.document ??= { documentElement: { toggleAttribute: () => {} } };
const { planStepsPreview, PLAN_STEPS_PREVIEW_LIMIT } = await import(pathToFileURL(out).href);

assert.equal(PLAN_STEPS_PREVIEW_LIMIT, 12);

// A brief plan (many sections: 40+ steps, RES-01's own fixture size) —
// only the first N are shown, the rest counted, not dropped.
const many = Array.from({ length: 43 }, (_, i) => `step-${i}`);
const long = planStepsPreview(many);
assert.equal(long.shown.length, 12);
assert.equal(long.hidden, 31);
assert.deepEqual(long.shown, many.slice(0, 12));
// The tail is still reachable — nothing here truncates the source array,
// only what one render pass shows before "Show all" is clicked.
assert.equal(many[42], 'step-42');

// A short plan: nothing hidden, nothing sliced away.
const short = planStepsPreview(['a', 'b', 'c']);
assert.equal(short.hidden, 0);
assert.deepEqual(short.shown, ['a', 'b', 'c']);

// Exactly at the limit: still nothing hidden (no "+0 more" button).
const exact = planStepsPreview(Array.from({ length: 12 }, (_, i) => i));
assert.equal(exact.hidden, 0);
assert.equal(exact.shown.length, 12);

// A custom limit is honoured.
const custom = planStepsPreview(['a', 'b', 'c', 'd', 'e'], 2);
assert.equal(custom.hidden, 3);
assert.deepEqual(custom.shown, ['a', 'b']);

console.log('ok l71-plan-steps-preview');
