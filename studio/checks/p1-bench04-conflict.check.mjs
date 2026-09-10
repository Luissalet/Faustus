// BENCH-04 - three-way save-conflict comparison
// (studio/src/adapters/fileConflict.ts).
//
// Bundled with esbuild on the fly; run by tests/test_p1_bench04_conflict_js.py,
// or by hand:
//   node studio/checks/p1-bench04-conflict.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-conflict-'));
async function load(rel, name) {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', rel)], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const m = await load(join('adapters', 'fileConflict.ts'), 'fileConflict.mjs');

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };

// ── lines only I changed vs only the other save changed vs both ──
{
  const base = 'a\nb\nc\nd';
  const mine = 'A\nb\nc\nd';   // only line 1 changed
  const theirs = 'a\nb\nC\nd'; // only line 3 changed
  const rows = m.threeWayLines(base, mine, theirs);
  assert(rows[0].status === 'mine-only', 'line only I touched is mine-only');
  assert(rows[1].status === 'same', 'untouched line is same');
  assert(rows[2].status === 'theirs-only', 'line only the other save touched is theirs-only');
  const sum = m.threeWaySummary(rows);
  assert(sum.mineOnly === 1 && sum.theirsOnly === 1 && sum.diverged === 0, `summary counts right: ${JSON.stringify(sum)}`);
}

// ── both sides changed the SAME line to the SAME text: not a real conflict ──
{
  const rows = m.threeWayLines('x', 'y', 'y');
  assert(rows[0].status === 'same', 'identical edits on both sides are not a divergence');
}

// ── both sides changed the same line differently: the one row that needs a
//    person's judgement ──
{
  const rows = m.threeWayLines('x', 'mine-edit', 'their-edit');
  assert(rows[0].status === 'diverged', 'different edits to the same line diverge');
  const sum = m.threeWaySummary(rows);
  assert(sum.diverged === 1, 'and the summary counts it');
}

// ── nothing is dropped: base/mine/theirs of different lengths still line up ──
{
  const rows = m.threeWayLines('a\nb', 'a\nb\nc', 'a\nb');
  assert(rows.length === 3, 'the longer side sets the row count');
  assert(rows[2].mine === 'c' && rows[2].theirs === null, 'a line only one side added shows, not silently trimmed');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
