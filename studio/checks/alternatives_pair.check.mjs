// OBJ-47: comparing two alternatives (studio/src/lib/altpair.ts) -- reading
// the server's unified diff into numbered rows, laying it out side by side,
// filtering and moving through the file list, choosing the pair, and the
// wiring of the screen (adapter call, i18n rows, no horizontal scroll outside
// the diff viewer). Bundled with esbuild on the fly; run by
// tests/test_studio_alternatives_pair_js.py, or by hand:
//   node studio/checks/alternatives_pair.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-altpair-')), 'altpair.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'lib', 'altpair.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const a = await import(pathToFileURL(out).href);
const read = (p) => readFileSync(join(root, p), 'utf8').replace(/\r\n/g, '\n');

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};
const eq = (x, y, msg) => assert(JSON.stringify(x) === JSON.stringify(y), `${msg} (got ${JSON.stringify(x)})`);

const DIFF = [
  '--- a/x.py', '+++ b/x.py',
  '@@ -1,4 +1,5 @@',
  ' keep', '-old one', '-old two', '+new one', '+new two', '+new three', ' tail',
  '\\ No newline at end of file',
].join('\n');

// ── parsing ──
{
  const rows = a.parseUnifiedDiff(DIFF);
  eq(rows.map((r) => r.kind), ['hunk', 'ctx', 'del', 'del', 'add', 'add', 'add', 'ctx', 'note'], 'rows by kind, headers dropped');
  eq([rows[1].oldNo, rows[1].newNo], [1, 1], 'a context line carries both numbers');
  eq([rows[2].oldNo, rows[2].newNo], [2, null], 'a removed line has only the old number');
  eq([rows[4].oldNo, rows[4].newNo], [null, 2], 'an added line has only the new number');
  eq([rows[7].oldNo, rows[7].newNo], [4, 5], 'numbering continues after a changed block');
  eq(rows[8].text, 'No newline at end of file', 'the end-of-file marker keeps its words');
  eq(a.parseUnifiedDiff(''), [], 'no diff, no rows');
  const tricky = a.parseUnifiedDiff(['--- a/f', '+++ b/f', '@@ -1,2 +1,1 @@', '--- looks like a header', '+++ so does this', ' x'].join('\n'));
  eq(tricky.map((r) => [r.kind, r.text]), [['hunk', '@@ -1,2 +1,1 @@'], ['del', '-- looks like a header'], ['add', '++ so does this'], ['ctx', 'x']],
    'a removed line that starts with dashes is content, not a header');
  const two = a.parseUnifiedDiff(['--- a/f', '+++ b/f', '@@ -1 +1 @@', '-a', '+b', '@@ -40 +40 @@', '-c', '+d'].join('\n'));
  eq([two[4].oldNo, two[4].newNo], [40, null], 'a second hunk restarts the numbering from its own header');
  eq(a.parseUnifiedDiff('--- /dev/null\n+++ b/n\n@@ -0,0 +1,2 @@\n+one\n+two\n').map((r) => r.newNo), [null, 1, 2], 'a new file starts at line 1 and the trailing newline is no row');
}

// ── side by side ──
{
  const side = a.toSideBySide(a.parseUnifiedDiff(DIFF));
  eq(side.length, 1 + 1 + 3 + 1 + 1, 'hunk + context + 3 change rows (2 paired, 1 padded) + context + note');
  eq([side[2].left.kind, side[2].right.kind, side[2].left.text, side[2].right.text], ['del', 'add', 'old one', 'new one'], 'a removed line sits beside the added one');
  eq([side[4].left.kind, side[4].right.kind, side[4].right.text], ['empty', 'add', 'new three'], 'the shorter side is padded with an empty cell');
  eq([side[1].left.no, side[1].right.no], [1, 1], 'context shows on both sides');
  eq(side[0].hunk, '@@ -1,4 +1,5 @@', 'the hunk header spans the row');
  eq(side[6].note, 'No newline at end of file', 'the marker is its own row');
  const onlyAdd = a.toSideBySide(a.parseUnifiedDiff('--- /dev/null\n+++ b/n\n@@ -0,0 +1,1 @@\n+one'));
  eq([onlyAdd[1].left.kind, onlyAdd[1].right.kind], ['empty', 'add'], 'a new file is all right-hand side');
}

// ── file list ──
const f = (path, status, overlap = null, extra = {}) => ({
  path, old_path: null, status, size_a: 1, size_b: 1, binary: false, too_large: false, additions: 1, deletions: 0,
  diff: 'x', diff_truncated: false, omitted_lines: 0, diff_total_lines: 1, touched_by: ['a'], base_change: { a: null, b: null }, overlap, ...extra,
});
{
  const files = [
    f('src/App.tsx', 'changed', 'conflict'), f('docs/Readme.md', 'added'), f('src/new.ts', 'renamed', null, { old_path: 'src/old.ts' }),
    f('gone.txt', 'removed'), f('src/util.ts', 'changed', 'mergeable'),
  ];
  eq(a.filterFiles(files, { query: '', status: 'all' }).length, 5, 'no filter keeps everything');
  eq(a.filterFiles(files, { query: 'SRC/', status: 'all' }).map((x) => x.path), ['src/App.tsx', 'src/new.ts', 'src/util.ts'], 'search is case-insensitive on the path');
  eq(a.filterFiles(files, { query: 'old.ts', status: 'all' }).map((x) => x.path), ['src/new.ts'], 'a renamed file is found by its old name too');
  eq(a.filterFiles(files, { query: '', status: 'conflict' }).map((x) => x.path), ['src/App.tsx'], 'the conflict chip filters on the overlap verdict');
  eq(a.filterFiles(files, { query: '', status: 'changed' }).length, 2, 'a status chip filters on status');
  eq(a.filterFiles(files, { query: 'nothing', status: 'all' }), [], 'no match, empty list');
  eq(a.statusCounts(files), { all: 5, added: 1, removed: 1, changed: 2, renamed: 1, conflict: 1 }, 'chip counts');
  eq(a.keepSelection(files, 'gone.txt'), 'gone.txt', 'the selected file stays selected');
  eq(a.keepSelection(files, 'filtered-out.txt'), 'src/App.tsx', 'a selection that vanished falls to the first file');
  eq(a.keepSelection([], 'x'), null, 'an empty list selects nothing');
}

// ── keyboard movement ──
{
  eq(a.moveIndex(0, 'ArrowDown', 3), 1, 'down moves one');
  eq(a.moveIndex(2, 'ArrowDown', 3), 2, 'down stops at the last');
  eq(a.moveIndex(0, 'ArrowUp', 3), 0, 'up stops at the first');
  eq(a.moveIndex(1, 'ArrowUp', 3), 0, 'up moves one');
  eq(a.moveIndex(1, 'Home', 3), 0, 'Home goes to the first');
  eq(a.moveIndex(1, 'End', 3), 2, 'End goes to the last');
  eq(a.moveIndex(1, 'a', 3), 1, 'another key changes nothing');
  eq(a.moveIndex(7, 'ArrowUp', 3), 1, 'an index past the end is pulled back into the list');
  eq(a.moveIndex(0, 'ArrowDown', 0), -1, 'an empty list has no index');
}

// ── choosing the pair ──
{
  const ids = ['a1', 'a2', 'a3'];
  eq(a.defaultPair(['a1'], null), null, 'one alternative cannot be compared');
  eq(a.defaultPair(ids, null), ['a1', 'a2'], 'the first two by default');
  eq(a.defaultPair(ids, ['a3', 'a1']), ['a3', 'a1'], 'a valid choice is kept');
  eq(a.defaultPair(ids, ['a3', 'gone']), ['a1', 'a2'], 'a deleted alternative resets the pair');
  eq(a.defaultPair(ids, ['a2', 'a2']), ['a1', 'a2'], 'the same alternative twice is never kept');
  eq(a.pickSide(['a1', 'a2'], 0, 'a3', ids), ['a3', 'a2'], 'pick a free alternative');
  eq(a.pickSide(['a1', 'a2'], 0, 'a2', ids), ['a2', 'a1'], 'picking the other side swaps instead of duplicating');
  eq(a.pickSide(['a1', 'a2'], 1, 'a1', ids), ['a2', 'a1'], 'same on the right');
  eq(a.swapPair(['a1', 'a2']), ['a2', 'a1'], 'swap');
}

// ── small formatters ──
{
  eq([a.formatBytes(0), a.formatBytes(1023), a.formatBytes(1536), a.formatBytes(20480), a.formatBytes(3 * 1024 * 1024)], ['0 B', '1023 B', '1.5 KB', '20 KB', '3.0 MB'], 'sizes');
  eq(a.formatBytes(null), '—', 'a side that does not have the file has no size');
  eq([a.noDiffReason(f('b', 'changed', null, { binary: true })), a.noDiffReason(f('l', 'changed', null, { too_large: true })),
    a.noDiffReason(f('r', 'renamed', null, { diff: '' })), a.noDiffReason(f('e', 'changed', null, { diff: '' })), a.noDiffReason(f('d', 'changed'))],
  ['binary', 'too_large', 'same_content', 'empty', null], 'why there is no diff to show');
  eq(['conflict', 'mergeable', 'identical', null].map(a.overlapTone), ['bad', 'warn', 'good', 'quiet'], 'overlap tones');
  eq(['added', 'removed', 'renamed', 'changed'].map(a.statusGlyph), ['+', '−', '→', '~'], 'status glyphs');
}

// ── wiring: adapter, screen, i18n, layout rules ──
{
  const adapter = read('studio/src/adapters/alternatives.ts');
  assert(adapter.includes('export function comparePair') && adapter.includes('/compare/${encodeURIComponent(altA)}/${encodeURIComponent(altB)}'), 'adapter calls the pair endpoint');
  assert(adapter.includes('export interface PairResult') && adapter.includes('diff_truncated') && adapter.includes('too_large'), 'adapter types carry the truncation fields');

  const view = read('studio/src/screens/alternatives/PairDiff.tsx');
  assert(!/\bfetch\(/.test(view), 'PairDiff reads through the adapter, not fetch()');
  assert(view.includes('comparePair') && view.includes('toSideBySide') && view.includes('parseUnifiedDiff'), 'PairDiff uses the adapter and the pure logic');
  assert(view.includes('aria-pressed') && view.includes('onKeyDown') && view.includes('tabIndex={0}'), 'keyboard: toggles are buttons with state, the list handles arrows, the viewer is focusable');
  assert(view.includes('role="region"') && view.includes('aria-live'), 'the viewer is a labelled region and the result is announced');
  assert(!/#[0-9a-fA-F]{3,8}\b/.test(view), 'no hard-coded colours in the component');
  assert(read('studio/src/screens/alternatives/CompareView.tsx').includes('<PairDiff'), 'CompareView mounts PairDiff');

  const css = read('studio/src/screens/alternatives/alternatives.css');
  const pairCss = css.slice(css.indexOf('/* ── pair diff'));
  assert(pairCss.length > 100, 'the pair diff styles exist');
  assert(!/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/.test(pairCss), 'pair diff styles use tokens only');
  assert(/\.fs-alt__pair-viewer[^{]*\{[^}]*overflow-x:\s*auto/.test(pairCss), 'the diff viewer is the one place that scrolls sideways');
  assert(!/\.fs-alt__pair-files[^{]*\{[^}]*overflow-x:\s*auto/.test(pairCss), 'the file list does not scroll sideways');
  assert(/prefers-reduced-motion/.test(pairCss) || !/transition|animation/.test(pairCss), 'no motion that ignores reduced-motion');

  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  const used = [...view.matchAll(/(?<![A-Za-z_.])t\(\s*'((?:[^'\\]|\\.)*)'/g)].map((m) => m[1].replace(/\\'/g, "'"));
  assert(used.length > 20, `PairDiff has interface strings (${used.length})`);
  const missing = used.filter((k) => !keys.has(k));
  assert(missing.length === 0, `every PairDiff string has a Spanish row${missing.length ? ': ' + missing.join(' | ') : ''}`);
}

if (failed) {
  console.error(`${failed} FAILED`);
  process.exit(1);
}
console.log('ALL OK');
