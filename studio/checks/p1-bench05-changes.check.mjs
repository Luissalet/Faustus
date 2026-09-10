// BENCH-05 - "ChangeSet actual con diff por fichero" in the workbench
// (studio/src/adapters/workbenchChanges.ts).
//
// Bundled with esbuild on the fly; run by tests/test_p1_bench05_changes_js.py,
// or by hand:
//   node studio/checks/p1-bench05-changes.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-changes-'));
async function load(rel, name) {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', rel)], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const m = await load(join('adapters', 'workbenchChanges.ts'), 'workbenchChanges.mjs');

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };

// ── a file touched by two calls in the same turn is one row, not two ──
{
  const rows = m.aggregateFileChanges([
    { file: 'a.py', added: 3, removed: 1, newFile: false },
    { file: 'a.py', added: 2, removed: 0, newFile: false },
    { file: 'b.py', added: 0, removed: 5, newFile: false },
  ]);
  assert(rows.length === 2, 'two distinct files -> two rows');
  const a = rows.find((r) => r.file === 'a.py');
  assert(a.added === 5 && a.removed === 1 && a.edits === 2, `a.py totals accumulate: ${JSON.stringify(a)}`);
}

// ── most-changed file sorts first ──
{
  const rows = m.aggregateFileChanges([
    { file: 'small.py', added: 1, removed: 0, newFile: false },
    { file: 'big.py', added: 40, removed: 10, newFile: false },
  ]);
  assert(rows[0].file === 'big.py', 'the file with the most changed lines sorts first');
}

// ── a new file keeps that flag even if only one of several diffs set it ──
{
  const rows = m.aggregateFileChanges([
    { file: 'x.py', added: 1, removed: 0, newFile: true },
    { file: 'x.py', added: 2, removed: 0, newFile: false },
  ]);
  assert(rows[0].newFile === true, 'newFile is sticky across edits to the same file');
}

// ── an empty diff list is an empty result, not a crash ──
assert(m.aggregateFileChanges([]).length === 0, 'no diffs -> no rows');

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
