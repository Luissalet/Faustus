// Studio tool card: a long tool output is never cut in silence, and the
// "back to the bottom" pill only says "New messages" when some arrived.
// Drives the real Transcript.tsx helpers through esbuild, same pattern as
// l65-transcript-helpers.check.mjs.
//
//   - `outputPreview`: what the card paints (6,000 chars folded, up to
//     200,000 unfolded), whether that is all of it, and never half an emoji.
//   - `transcriptMark` / `hasArrivedSince`: unfolding a card or scrolling
//     leaves the mark alone; a new turn or the last one growing changes it.
//   - The copy each state reads has a Spanish row (docs/ui/i18n/es.tsv).
//
// Run by tests/test_studio_tool_output_js.py, or by hand:
//   node studio/checks/tool-output-preview.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-tool-output-')), 'transcript.mjs');
await build({
  entryPoints: [join(root, 'studio/src/screens/studio/Transcript.tsx')], bundle: true,
  platform: 'node', format: 'esm', jsx: 'automatic', outfile: out, logLevel: 'silent',
});
// Same minimal stand-ins as l65-transcript-helpers: shell/display.ts touches
// `document`/`localStorage` once at module load.
globalThis.window ??= { localStorage: { getItem: () => null, setItem: () => {} } };
globalThis.document ??= { documentElement: { toggleAttribute: () => {} } };
const {
  outputPreview, OUTPUT_PREVIEW_CHARS, OUTPUT_EXPANDED_CHARS, transcriptMark, hasArrivedSince, toolUseSummary,
} = await import(pathToFileURL(out).href);

assert.equal(OUTPUT_PREVIEW_CHARS, 6000);
assert.equal(OUTPUT_EXPANDED_CHARS, 200000);

// ── Short output: shown whole, no indicator ──
const short = 'x'.repeat(OUTPUT_PREVIEW_CHARS);
assert.deepEqual(outputPreview(short, false), { shown: short, total: 6000, truncated: false, long: false });

// ── The real case: a 32,678-char inspection result ──
const big = 'a'.repeat(32678);
const folded = outputPreview(big, false);
assert.equal(folded.shown.length, 6000);
assert.equal(folded.total, 32678);
assert.equal(folded.truncated, true, 'a cut output must say it was cut');
assert.equal(folded.long, true);
const unfolded = outputPreview(big, true);
assert.equal(unfolded.shown, big, 'unfolding shows every character');
assert.equal(unfolded.truncated, false);
assert.equal(unfolded.long, true, 'the row stays so the reader can fold it back and copy it');

// ── Megabytes: unfolding still caps what is painted ──
const huge = 'b'.repeat(1_500_000);
const hugeOpen = outputPreview(huge, true);
assert.equal(hugeOpen.shown.length, OUTPUT_EXPANDED_CHARS);
assert.equal(hugeOpen.truncated, true);
assert.equal(hugeOpen.total, 1_500_000);

// ── Never half a surrogate pair at the cut ──
const emoji = 'c'.repeat(OUTPUT_PREVIEW_CHARS - 1) + '\u{1F600}' + 'd'.repeat(50);
const cut = outputPreview(emoji, false);
assert.equal(cut.shown.length, OUTPUT_PREVIEW_CHARS - 1);
assert.ok(!/[\uD800-\uDBFF]$/.test(cut.shown), 'no lone high surrogate at the end');

// ── Rail copy for a non-shell tool ──
assert.deepEqual(toolUseSummary(1, false), { one: 'Used 1 tool', other: 'Used {n} tools', n: 1 });
assert.deepEqual(toolUseSummary(2, true), { one: 'Using a tool', other: 'Using {n} tools', n: 2 });

// ── "New messages" only when something arrived ──
const turnA = { id: 'u1', text: 'Inspecciona defensa.pptx', steps: [] };
const turnB = { id: 'a1', text: 'La defensa no conserva', steps: [{}, {}] };
const before = transcriptMark([turnA, turnB]);
assert.equal(transcriptMark([turnA, turnB]), before, 'same transcript, same mark (unfolding a card changes nothing)');
assert.equal(hasArrivedSince(before, before), false);
assert.equal(hasArrivedSince(null, before), false, 'at the bottom there is nothing to announce');
assert.equal(hasArrivedSince(before, transcriptMark([turnA, turnB, { id: 'u2', text: 'y?', steps: [] }])), true, 'a new turn');
assert.equal(hasArrivedSince(before, transcriptMark([turnA, { ...turnB, text: turnB.text + ' la plantilla.' }])), true, 'the last turn streamed more text');
assert.equal(hasArrivedSince(before, transcriptMark([turnA, { ...turnB, steps: [{}, {}, {}] }])), true, 'another tool step');
assert.equal(transcriptMark([]), '0');

// ── Every new string has its Spanish row ──
const tsv = new Set(readFileSync(join(root, 'docs/ui/i18n/es.tsv'), 'utf8').split(/\r?\n/).map((l) => l.split('\t')[0]));
for (const key of [
  'Showing {shown} of {total} characters', 'Showing all {total} characters', 'Show full output', 'Show less',
  'Copy full output', 'Used 1 tool', 'Used {n} tools', 'Using a tool', 'Using {n} tools', 'Latest messages',
  'New messages', 'New messages: jump to the latest', 'Jump to the latest messages', 'Inspect deliverable',
  'Look up tools', 'View the file', 'Open the document',
]) {
  assert.ok(tsv.has(key), `es.tsv has no row for ${JSON.stringify(key)}`);
}

// ── The card no longer slices the output itself ──
const src = readFileSync(join(root, 'studio/src/screens/studio/Transcript.tsx'), 'utf8');
assert.ok(!/output\.slice\(0,\s*6000\)/.test(src), 'the silent 6000-char slice is gone');
assert.ok(src.includes('<ToolOutput text={step.output} />'), 'the card renders through ToolOutput');

console.log('ok tool-output-preview');
