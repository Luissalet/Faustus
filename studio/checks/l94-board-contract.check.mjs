// Lote 94 (OBJ-6) — Studio<->backend board contract verification.
//
// tests/test_l94_board_contract.py starts routes/board_routes.py under a
// real FastAPI TestClient, creates real issues (with comments, links, refs,
// a blocking relationship), and captures the ACTUAL JSON bodies the server
// returns for `listIssues`, `getIssue`, `getSummary` and one 404. Those
// captured bodies — not hand-typed fixtures that could quietly drift from
// what the server really sends — are written to a JSON file and handed to
// this script, which replays them through a real `fetch` (Node's own
// `Response`, so `.clone()`/`.json()`/`.ok` all behave exactly like a
// browser's) and asserts `studio/src/adapters/board.ts`'s own functions
// parse them the way Studio's screens assume: the right top-level shape,
// `error_class` sitting at the TOP level of an error body (not buried under
// `detail.code` — see routes/board_routes.py's `_error()`), and the fields
// IssueCompact/Issue declare actually being there.
//
// Bundled with esbuild on the fly, same `load()` helper as
// studio/checks/l93-board.check.mjs. Run by tests/test_l94_board_contract.py,
// or by hand once a fixtures file exists:
//   node studio/checks/l94-board-contract.check.mjs <fixtures.json>
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-board-contract-'));

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

const fixturesPath = process.argv[2];
if (!fixturesPath) {
  console.error('FAIL: usage: node l94-board-contract.check.mjs <fixtures.json>');
  process.exit(1);
}
const fixtures = JSON.parse(readFileSync(fixturesPath, 'utf-8'));
const { meta, responses } = fixtures;

// `responses` keys are `${method} ${path}` -- exactly how board.ts builds
// its own requests (see `base()`/`getBoard`/`sendBoard`/`deleteBoard`), so a
// mismatch here (an adapter change that alters a URL) fails loudly instead
// of silently hitting a fixture that happens not to exist.
globalThis.fetch = async (path, init) => {
  const method = (init && init.method) || 'GET';
  const key = `${method} ${path}`;
  const fixture = responses[key];
  if (!fixture) {
    throw new Error(`no captured fixture for ${key} -- board.ts built a URL the test never recorded`);
  }
  return new Response(JSON.stringify(fixture.body), {
    status: fixture.status,
    headers: { 'content-type': 'application/json' },
  });
};

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

// ── listIssues: the real /issues response parses, and every IssueCompact
// field the adapter's interface declares is actually present ──
{
  const res = await b.listIssues(meta.projectId);
  assert(Array.isArray(res.issues) && res.issues.length >= 2, 'listIssues returns the created issues');
  assert('next_cursor' in res, 'listIssues response carries next_cursor');
  const first = res.issues.find((i) => i.id === meta.issueAId);
  assert(!!first, 'listIssues includes the issue this test created');
  for (const field of ['id', 'type', 'title', 'status', 'priority', 'assignee', 'labels', 'updated_at', 'blocked_by']) {
    assert(field in first, `compact issue has field "${field}"`);
  }
  assert(Array.isArray(first.labels), 'compact issue.labels is an array');
  assert(Array.isArray(first.blocked_by), 'compact issue.blocked_by is an array');
  assert(typeof first.assignee === 'string', 'compact issue.assignee is always a string on the wire, never null');
}

// ── getIssue: the full issue -- comments/links/refs/events all present and
// shaped the way Issue/IssueComment/IssueLink/IssueRef declare ──
{
  const res = await b.getIssue(meta.projectId, meta.issueAId);
  const issue = res.issue;
  assert(issue.id === meta.issueAId, 'getIssue returns the requested issue');
  for (const field of ['body_md', 'created_at', 'closed_at', 'created_by', 'comments', 'events', 'links', 'refs']) {
    assert(field in issue, `full issue has field "${field}"`);
  }
  assert(issue.comments.length >= 1, 'getIssue carries the comment this test posted');
  assert(issue.comments[0].body_md === meta.commentBody, 'the comment body round-trips exactly');
  assert(issue.refs.length >= 1 && issue.refs[0].kind === 'commit', 'getIssue carries the commit ref this test posted');
  assert(issue.blocked_by.includes(meta.issueBId), 'the blocking link this test posted shows up as blocked_by');
}

// ── getSummary: the compact projection injected into the agent's prompt --
// the same shape src.project_board.summary()/routes/board_routes.py build ──
{
  const res = await b.getSummary(meta.projectId);
  assert(res.key === meta.boardKey, `summary carries the project's own board key (got ${res.key})`);
  for (const field of ['counts', 'ready', 'in_progress', 'recent_done']) {
    assert(field in res, `summary has field "${field}"`);
  }
  assert(Array.isArray(res.ready), 'summary.ready is an array');
}

// ── A REAL 404 from board_routes.py: error_class must sit at the TOP level
// of the thrown BoardApiError, not nested under detail.code -- this is
// exactly the routes/board_routes.py bug Lote 94 fixed (HTTPException with a
// dict detail used to bury the specific `board.not_found` class where
// core.middleware's OBS-03 middleware could never see it, backfilling a
// generic status-derived class instead). ──
{
  let caught = null;
  try {
    await b.getIssue(meta.projectId, 'NOPE-999');
  } catch (e) {
    caught = e;
  }
  assert(caught !== null, 'getIssue on an unknown id rejects');
  assert(caught && caught.name === 'BoardApiError', 'the rejection is a BoardApiError');
  assert(caught && caught.errorClass === 'board.not_found', `errorClass is the specific board.not_found class (got ${caught && caught.errorClass})`);
  assert(caught && caught.status === 404, 'the HTTP status is 404');
}

if (failed > 0) {
  console.error(`\n${failed} check(s) failed`);
  process.exit(1);
}
console.log('\nall checks passed');
