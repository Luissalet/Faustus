// Lote 89 (OBJ-4) — merge and branch deletion: the request shape `merge()`,
// `mergeAbort()` and `deleteBranch()` (studio/src/adapters/git.ts) build,
// and how a 409 `git.merge_conflict` surfaces as a typed `GitApiError`.
// `fetch` is mocked (Node's own global, so real `Response` objects work
// unmodified) rather than hitting a server — none of this touches the DOM.
//
// Bundled with esbuild on the fly; run by tests/test_l89_git_merge_js.py,
// or by hand:
//   node studio/checks/l89-git-merge.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-git-merge-'));

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

const g = await load(join('adapters', 'git.ts'), 'git-merge.mjs');

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

/** Replaces `globalThis.fetch` with a recorder + the given canned response,
 *  and returns the array of {url, init} it was called with. */
function mockFetch(respond) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return respond();
  };
  return calls;
}

const ok = (body, status = 200) => new Response(JSON.stringify(body), { status });

// ── merge(): defaults, POST shape ──
{
  const calls = mockFetch(() => ok({ ok: true, sha: 'abc123', fast_forward: true, conflicts: [], repo: { id: 'r1' } }));
  const res = await g.merge('r1', { branch: 'feature' });
  assert(calls.length === 1, 'merge() makes exactly one request');
  assert(calls[0].url === '/api/git/repos/r1/merge', "merge() posts to the repo's merge route");
  assert(calls[0].init.method === 'POST', 'merge() is a POST');
  const body = JSON.parse(calls[0].init.body);
  assert(body.branch === 'feature', 'branch is sent as given');
  assert(body.ff === 'auto', 'ff defaults to auto (fast-forward when possible)');
  assert(body.keep_conflicts === false, 'keep_conflicts defaults to false');
  assert(body.message === undefined, 'no message given means none is sent');
  assert(res.sha === 'abc123' && res.fast_forward === true, "merge() returns the server's parsed result");
}

// ── merge(): every option passed through, camelCase -> snake_case ──
{
  const calls = mockFetch(() => ok({ ok: true, sha: 'x', fast_forward: false, conflicts: [], repo: {} }));
  await g.merge('r1', { branch: 'feature', ff: 'no', message: 'merge it', keepConflicts: true });
  const body = JSON.parse(calls[0].init.body);
  assert(body.ff === 'no', "ff: 'no' is passed through (always a merge commit)");
  assert(body.message === 'merge it', 'message is passed through');
  assert(body.keep_conflicts === true, 'keepConflicts maps to keep_conflicts');
}

// ── mergeAbort(): POST to .../merge/abort ──
{
  const calls = mockFetch(() => ok({ ok: true, repo: {} }));
  await g.mergeAbort('r1');
  assert(calls[0].url === '/api/git/repos/r1/merge/abort', 'mergeAbort() posts to the abort route');
  assert(calls[0].init.method === 'POST', 'mergeAbort() is a POST');
}

// ── deleteBranch(): DELETE, no query string when force/remote are both off ──
{
  const calls = mockFetch(() => ok({ ok: true, deleted: 'topic', repo: {} }));
  await g.deleteBranch('r1', 'topic');
  assert(calls[0].url === '/api/git/repos/r1/branches/topic', 'deleteBranch() with no options has no query string');
  assert(calls[0].init.method === 'DELETE', 'deleteBranch() is a DELETE');
}

// ── deleteBranch(): a slash in the name round-trips through the URL, and
// force/remote become query params ──
{
  const calls = mockFetch(() => ok({ ok: true, deleted: 'feature/x', remote_deleted: true, repo: {} }));
  await g.deleteBranch('r1', 'feature/x', { force: true, remote: true });
  assert(
    calls[0].url === '/api/git/repos/r1/branches/feature%2Fx?force=1&remote=1',
    'deleteBranch() encodes a slash in the branch name and appends force=1&remote=1',
  );
}

// ── merge(): a 409 git.merge_conflict surfaces as a typed GitApiError,
// never a generic thrown Error ──
{
  mockFetch(() => ok({
    error_class: 'git.merge_conflict', detail: 'paused with conflicts', conflicts: ['a.txt'], aborted: true, repo: {},
  }, 409));
  try {
    await g.merge('r1', { branch: 'other' });
    assert(false, 'a 409 response must reject the promise');
  } catch (e) {
    assert(e.name === 'GitApiError', 'the rejection is a GitApiError, not a plain Error');
    assert(e.errorClass === 'git.merge_conflict', 'errorClass is read off error_class in the body');
    assert(Array.isArray(e.payload.conflicts) && e.payload.conflicts[0] === 'a.txt', 'payload carries the conflicting paths');
    assert(e.payload.aborted === true, 'payload carries whether the merge was auto-aborted');
  }
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nGit merge / delete branch (lote 89): all checks passed');
