// Lote 93 (OBJ-6) — Project board's pure logic (studio/src/adapters/board.ts):
// kanban column grouping, the toolbar filter, ready ordering (priority then
// antiquity), allowed status transitions and the issue-id chip regex/splitter
// used by Transcript.tsx and CommitGraph.tsx. None of this touches the DOM
// or fetch, so it is exercised here directly rather than through a browser.
//
// Bundled with esbuild on the fly; run by tests/test_l93_board_js.py, or by
// hand:
//   node studio/checks/l93-board.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-board-'));

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

const b = await load(join('adapters', 'board.ts'), 'board.mjs');

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};
const deepEqual = (a, b2) => JSON.stringify(a) === JSON.stringify(b2);

const issue = (over) => ({
  id: 'FAU-1', type: 'task', title: 'x', status: 'open', priority: 'P2',
  assignee: null, labels: [], updated_at: '2026-01-01T00:00:00Z', blocked_by: [],
  ...over,
});

// ── groupIssuesByColumn: every status lands in exactly one column; wontfix/duplicate fold together ──
{
  const issues = [
    issue({ id: 'FAU-1', status: 'open' }),
    issue({ id: 'FAU-2', status: 'in_progress' }),
    issue({ id: 'FAU-3', status: 'blocked' }),
    issue({ id: 'FAU-4', status: 'done' }),
    issue({ id: 'FAU-5', status: 'wontfix' }),
    issue({ id: 'FAU-6', status: 'duplicate' }),
  ];
  const grouped = b.groupIssuesByColumn(issues);
  assert(deepEqual(Object.keys(grouped).sort(), ['blocked', 'closed', 'done', 'in_progress', 'open'].sort()), `five columns, got ${JSON.stringify(Object.keys(grouped))}`);
  assert(grouped.open.length === 1 && grouped.open[0].id === 'FAU-1', 'open issue lands in the open column');
  assert(grouped.closed.length === 2, 'wontfix and duplicate fold into one closed column');
  assert(grouped.closed.map((i) => i.id).sort().join(',') === 'FAU-5,FAU-6', 'the closed column holds exactly the wontfix+duplicate issues');
  assert(deepEqual(b.groupIssuesByColumn([]).open, []), 'no issues still yields every column, empty');
}

// ── columnOf ──
{
  assert(b.columnOf('blocked') === 'blocked', 'blocked maps to its own column');
  assert(b.columnOf('wontfix') === 'closed', 'wontfix folds into "closed"');
  assert(b.columnOf('duplicate') === 'closed', 'duplicate folds into "closed"');
}

// ── filterIssues: text (title or id), type, priority, assignee — all AND'ed ──
{
  const issues = [
    issue({ id: 'FAU-1', title: 'Fix the login retry', type: 'bug', priority: 'P0', assignee: 'agent' }),
    issue({ id: 'FAU-2', title: 'Add dark mode toggle', type: 'feature', priority: 'P2', assignee: 'user' }),
    issue({ id: 'FAU-3', title: 'Investigate flaky test', type: 'task', priority: 'P1', assignee: null }),
  ];
  assert(b.filterIssues(issues, {}).length === 3, 'no filter keeps everything');
  assert(deepEqual(b.filterIssues(issues, { text: 'dark' }).map((i) => i.id), ['FAU-2']), 'text matches the title, case-insensitively');
  assert(deepEqual(b.filterIssues(issues, { text: 'fau-3' }).map((i) => i.id), ['FAU-3']), 'text also matches the issue id, case-insensitively');
  assert(deepEqual(b.filterIssues(issues, { type: 'bug' }).map((i) => i.id), ['FAU-1']), 'type filter is exact');
  assert(deepEqual(b.filterIssues(issues, { priority: 'P1' }).map((i) => i.id), ['FAU-3']), 'priority filter is exact');
  assert(deepEqual(b.filterIssues(issues, { assignee: 'user' }).map((i) => i.id), ['FAU-2']), 'assignee filter is exact');
  assert(b.filterIssues(issues, { text: 'dark', type: 'bug' }).length === 0, 'filters combine with AND, not OR');
}

// ── compareByPriorityThenAge: priority first, then oldest first ──
{
  const a = issue({ id: 'A', priority: 'P1', updated_at: '2026-02-01T00:00:00Z' });
  const c = issue({ id: 'C', priority: 'P0', updated_at: '2026-03-01T00:00:00Z' });
  const older = issue({ id: 'OLD', priority: 'P1', updated_at: '2026-01-01T00:00:00Z' });
  const sorted = [a, c, older].sort(b.compareByPriorityThenAge);
  assert(deepEqual(sorted.map((i) => i.id), ['C', 'OLD', 'A']), `P0 first regardless of age, then P1 oldest-first — got ${sorted.map((i) => i.id)}`);
  assert(b.compareByPriorityThenAge(a, a) === 0, 'identical issues compare equal');
}

// ── allowedNextStatuses / canTransition: done/wontfix/duplicate only reopen to open/in_progress ──
{
  assert(deepEqual(b.allowedNextStatuses('open').sort(), ['blocked', 'done', 'duplicate', 'in_progress', 'open', 'wontfix'].sort()), 'an open issue may move to any status');
  assert(deepEqual(b.allowedNextStatuses('done').sort(), ['done', 'in_progress', 'open'].sort()), 'done only reopens to open/in_progress (or stays done)');
  assert(deepEqual(b.allowedNextStatuses('wontfix').sort(), ['in_progress', 'open', 'wontfix'].sort()), 'wontfix only reopens to open/in_progress');
  assert(deepEqual(b.allowedNextStatuses('duplicate').sort(), ['duplicate', 'in_progress', 'open'].sort()), 'duplicate only reopens to open/in_progress');
  assert(b.canTransition('done', 'blocked') === false, 'done cannot move straight to blocked');
  assert(b.canTransition('done', 'open') === true, 'done can reopen to open');
  assert(b.canTransition('open', 'blocked') === true, 'open can move to blocked');
  assert(b.canTransition('open', 'open') === true, 'a status always "transitions" to itself (no-op drop)');
}

// ── issueIdRegex / linkIssueIds: only ids matching THIS project's key, word-bounded ──
{
  const re = b.issueIdRegex('FAU');
  assert('FAU-12 and FAU-9'.match(re).length === 2, 'matches every FAU-N in the text');
  assert('AFAU-12'.match(re) === null, 'never matches inside a longer token (prefix)');
  assert('FAU-123x'.match(re) === null, 'never matches inside a longer token (suffix)');
  assert('WH-4'.match(re) === null, 'a different project key never matches');

  const segs = b.linkIssueIds('See FAU-12 and also FAU-9, thanks', 'FAU');
  assert(deepEqual(segs, [
    { kind: 'text', text: 'See ' },
    { kind: 'issue', id: 'FAU-12' },
    { kind: 'text', text: ' and also ' },
    { kind: 'issue', id: 'FAU-9' },
    { kind: 'text', text: ', thanks' },
  ]), `splits text/issue runs in order — got ${JSON.stringify(segs)}`);

  assert(deepEqual(b.linkIssueIds('nothing to see here', 'FAU'), [{ kind: 'text', text: 'nothing to see here' }]), 'no match is one plain text segment');
  assert(deepEqual(b.linkIssueIds('FAU-1', ''), [{ kind: 'text', text: 'FAU-1' }]), 'an empty key never matches — the feature is a no-op until a key is known');
  assert(deepEqual(b.linkIssueIds('', 'FAU'), []), 'empty text is no segments');
  assert(deepEqual(b.linkIssueIds('FAU-1FAU-2', 'FAU'), [{ kind: 'text', text: 'FAU-1FAU-2' }]), 'adjacent digits/letters break the word boundary — no false split');
}

// ── isValidBoardKey ──
{
  assert(b.isValidBoardKey('FAU') === true, '3 uppercase letters is valid');
  assert(b.isValidBoardKey('LOCAL') === true, '5 uppercase letters is valid');
  assert(b.isValidBoardKey('AB') === false, 'too short is invalid');
  assert(b.isValidBoardKey('ABCDEF') === false, 'too long is invalid');
  assert(b.isValidBoardKey('fau') === false, 'lowercase is invalid');
  assert(b.isValidBoardKey('FA1') === false, 'digits are invalid');
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nBoard: all checks passed');
