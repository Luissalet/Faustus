// Lote 81 (OBJ-4) — Source control's pure logic (studio/src/adapters/git.ts):
// commit-graph lane assignment, ref-chip parsing, the Commit button's
// enabled rule, unified-diff line typing and the branch popover's search
// filter. None of this touches the DOM or fetch, so it is exercised here
// directly rather than through a browser.
//
// Bundled with esbuild on the fly; run by tests/test_l81_source_control_js.py,
// or by hand:
//   node studio/checks/l81-source-control.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-source-control-'));

async function load(rel, name) {
  const out = join(dir, name);
  await build({
    entryPoints: [join(root, 'studio', 'src', rel)],
    bundle: true,
    format: 'esm',
    platform: 'node',
    outfile: out,
    logLevel: 'silent',
  });
  return import(pathToFileURL(out).href);
}

const g = await load(join('adapters', 'git.ts'), 'git.mjs');

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};
const deepEqual = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ── canCommit: message + staged, both required ──
{
  assert(g.canCommit('fix bug', 1) === true, 'a message and a staged file enables Commit');
  assert(g.canCommit('  ', 1) === false, 'a whitespace-only message does not enable Commit');
  assert(g.canCommit('fix bug', 0) === false, 'nothing staged does not enable Commit even with a message');
  assert(g.canCommit('', 0) === false, 'neither message nor staged files enables Commit');
}

// ── aheadBehindLabel: behind then ahead, empty when caught up ──
{
  assert(g.aheadBehindLabel(0, 0) === '', 'caught up shows no chip, not "↓0 ↑0"');
  assert(g.aheadBehindLabel(2, 0) === '↑2', 'ahead only');
  assert(g.aheadBehindLabel(0, 3) === '↓3', 'behind only');
  assert(g.aheadBehindLabel(1, 2) === '↓2 ↑1', 'behind is named before ahead — what a pull would bring, then what a push would send');
}

// ── parseRefs: "HEAD -> branch" splits into two chips; tag: and remote names classify ──
{
  const chips = g.parseRefs(['HEAD -> master', 'origin/master', 'tag: v1.0', 'dev']);
  assert(deepEqual(chips, [
    { kind: 'head', label: 'HEAD' },
    { kind: 'branch', label: 'master' },
    { kind: 'remote', label: 'origin/master' },
    { kind: 'tag', label: 'v1.0' },
    { kind: 'branch', label: 'dev' },
  ]), `HEAD-> splits, remote/tag/branch classify correctly — got ${JSON.stringify(chips)}`);
  assert(deepEqual(g.parseRefs([]), []), 'no refs is no chips');
  assert(deepEqual(g.parseRefs(['HEAD']), [{ kind: 'head', label: 'HEAD' }]), 'a bare detached HEAD is its own chip');
}

// ── computeGraphLanes: linear history stays in lane 0; a merge opens and rejoins a lane ──
{
  const c = (sha, parents, over) => ({ sha, short: sha.slice(0, 7), parents, author: 'a', email: 'a@x', date: '2026-01-01T00:00:00Z', message: 'm', body: '', refs: [], ...over });
  const linear = [c('c3', ['c2']), c('c2', ['c1']), c('c1', [])];
  const linearRows = g.computeGraphLanes(linear);
  assert(linearRows.every((r) => r.lane === 0), 'a straight line of commits never leaves lane 0');
  assert(linearRows[2].parentLanes.length === 0, 'the root commit has no parent line');

  // c4 (HEAD) merges c3 (mainline) and c2b (a side branch that only c2b names as its parent c1).
  const merge = [
    c('c4', ['c3', 'c2b']),
    c('c3', ['c1']),
    c('c2b', ['c1']),
    c('c1', []),
  ];
  const rows = g.computeGraphLanes(merge);
  const byLane = rows[0].parentLanes.map((p) => p.lane);
  assert(rows[0].lane === 0, 'the merge commit itself sits in lane 0 (nothing was waiting for it yet)');
  assert(byLane[0] === 0, 'the first parent (mainline) continues the merge commit\'s own lane');
  assert(byLane[1] !== byLane[0], 'the second parent (the merged-in branch) opens a lane of its own');
  const c2bLane = byLane[1];
  const c2bRow = rows.find((r) => r.sha === 'c2b');
  assert(c2bRow.lane === c2bLane, 'the side branch\'s own commit lands in the lane the merge opened for it, not a new one');
  const c1Row = rows.find((r) => r.sha === 'c1');
  assert(c1Row.parentLanes.length === 0 && c1Row.lane === 0, 'both lanes rejoin: c1 is every remaining parent, so it lands where lane 0 is waiting');

  assert(rows[0].continuesFromAbove === false, 'the very first row (HEAD) never continues a line from above — nothing was waiting for it');
  assert(c1Row.continuesFromAbove === true, 'c1 lands where lane 0 was already waiting for it, so its dot connects to the line above');
}

// ── parseUnifiedDiff: +/-/context/hunk/meta, header lines never mistaken for a removed line ──
{
  const text = [
    'diff --git a/f.txt b/f.txt',
    'index 000..111 100644',
    '--- a/f.txt',
    '+++ b/f.txt',
    '@@ -1,2 +1,2 @@',
    ' unchanged',
    '-old line',
    '+new line',
    '',
  ].join('\n');
  const lines = g.parseUnifiedDiff(text);
  assert(lines.length === 8, `no stray trailing blank line from the final newline — got ${lines.length} lines`);
  assert(lines[0].type === 'meta' && lines[3].type === 'meta', '"diff --git" / "+++" are metadata, not additions, even though "+++" starts with +');
  assert(lines[2].type === 'meta', '"--- a/f.txt" is metadata, not a removed line, even though it starts with -');
  assert(lines[4].type === 'hunk' && lines[4].text === '@@ -1,2 +1,2 @@', 'the hunk header keeps its text verbatim');
  assert(lines[5].type === 'context' && lines[5].text === 'unchanged', 'a context line loses its leading space, not its content');
  assert(lines[6].type === 'del' && lines[6].text === 'old line', 'a removed line is typed del with the leading - stripped');
  assert(lines[7].type === 'add' && lines[7].text === 'new line', 'an added line is typed add with the leading + stripped');
  assert(deepEqual(g.parseUnifiedDiff(''), []), 'an empty diff is an empty line list');
}

// ── filterBranches: case-insensitive substring, empty query keeps everything ──
{
  const branches = [{ name: 'master' }, { name: 'origin/dev' }, { name: 'feature/OBJ-4' }];
  assert(g.filterBranches(branches, '').length === 3, 'an empty search keeps every branch');
  assert(deepEqual(g.filterBranches(branches, 'dev'), [{ name: 'origin/dev' }]), 'substring match, case-insensitive');
  assert(deepEqual(g.filterBranches(branches, 'obj-4'), [{ name: 'feature/OBJ-4' }]), 'case-insensitive against the branch name\'s own case');
  assert(g.filterBranches(branches, 'nope').length === 0, 'no match is an empty list, not everything');
}

// ── fileStatusLabel: the one-letter git status code named in full ──
{
  assert(g.fileStatusLabel('A') === 'Added', 'A -> Added');
  assert(g.fileStatusLabel('M') === 'Modified', 'M -> Modified');
  assert(g.fileStatusLabel('D') === 'Deleted', 'D -> Deleted');
  assert(g.fileStatusLabel('R') === 'Renamed', 'R -> Renamed');
  assert(g.fileStatusLabel('C') === 'Copied', 'C -> Copied');
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nSource control: all checks passed');
