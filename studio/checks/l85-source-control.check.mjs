// Lote 85 (OBJ-4, tercera tanda) — Source control's full-width layout,
// dedupe and light-polling merge, and the GitHub (`gh`) create/publish
// flow's pure logic (studio/src/adapters/git.ts): none of this touches the
// DOM or fetch, so it is exercised here directly rather than through a
// browser.
//
// Bundled with esbuild on the fly; run by tests/test_l85_source_control_js.py,
// or by hand:
//   node studio/checks/l85-source-control.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-source-control-l85-'));

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

function repo(over) {
  return {
    id: 'r1',
    path: 'D:/LocalAI/odysseus',
    name: 'odysseus',
    project_id: 'p1',
    project_name: 'LocalAI',
    root_folder: 'D:/LocalAI',
    parent_repo_id: null,
    branch: 'master',
    detached: false,
    head_sha: 'aaa',
    upstream: 'origin/master',
    ahead: 0,
    behind: 0,
    dirty: { staged: 0, unstaged: 0, untracked: 0 },
    user: { name: 'Luissalet', email: 'l@x' },
    remotes: [{ name: 'origin', fetch_url: 'x', push_url: 'x' }],
    ...over,
  };
}

// ── dedupeRepos: same path -> one row, projects folded, first repo wins ──
{
  const a = repo({ id: 'a', path: 'D:/LocalAI/odysseus', project_id: 'p1', project_name: 'LocalAI' });
  const b = repo({ id: 'b', path: 'D:/LocalAI/odysseus', project_id: 'p2', project_name: "Writer's Hoard" });
  const out = g.dedupeRepos([a, b]);
  assert(out.length === 1, `two entries at the same path collapse to one — got ${out.length}`);
  assert(out[0].id === 'a', 'the first-seen repo stays the base (id/project_id/project_name compatibility)');
  assert(
    deepEqual(out[0].projects, [{ id: 'p1', name: 'LocalAI' }, { id: 'p2', name: "Writer's Hoard" }]),
    `both projects are folded into .projects, in first-seen order — got ${JSON.stringify(out[0].projects)}`,
  );

  const distinct = g.dedupeRepos([repo({ id: 'x', path: 'D:/A/one' }), repo({ id: 'y', path: 'D:/A/two' })]);
  assert(distinct.length === 2, 'repos at different paths are not merged');

  const already = repo({ id: 'z', path: 'D:/A/three', projects: [{ id: 'p1', name: 'LocalAI' }] });
  const noop = g.dedupeRepos([already]);
  assert(deepEqual(noop[0].projects, [{ id: 'p1', name: 'LocalAI' }]), 'a repo that already carries `projects` keeps it as-is when it is the only entry');

  const dupeSameProject = g.dedupeRepos([repo({ id: 'p', path: 'D:/A/four' }), repo({ id: 'q', path: 'D:/A/four' })]);
  assert(dupeSameProject[0].projects.length === 1, 'the exact same project named twice for one path is not duplicated in .projects');
}

// ── repoProjectsLabel: "LocalAI · Writer's Hoard", falls back to project_name ──
{
  assert(
    g.repoProjectsLabel({ projects: [{ id: 'p1', name: 'LocalAI' }, { id: 'p2', name: "Writer's Hoard" }], project_name: null }) === "LocalAI · Writer's Hoard",
    'multiple projects join with " · "',
  );
  assert(g.repoProjectsLabel({ projects: [], project_name: 'LocalAI' }) === 'LocalAI', 'an empty projects list falls back to project_name');
  assert(g.repoProjectsLabel({ projects: undefined, project_name: null }) === '', 'no projects at all is an empty string, never null/undefined');
}

// ── mergeLightRepos: only the light fields move, remotes/user/identity/policy hold ──
{
  const full = [
    repo({
      id: 'r1',
      branch: 'master',
      ahead: 0,
      behind: 0,
      identity: { id: 'i1', label: 'Luissalet', github_login: 'Luissalet' },
      policy: { effective: { use_branch: true, branch_prefix: 'agent/', commit: true, commit_message_prefix: '', push: false, push_set_upstream: false }, overridden: false },
    }),
  ];
  const light = [repo({ id: 'r1', branch: 'master', ahead: 2, behind: 1, dirty: { staged: 1, unstaged: 0, untracked: 0 }, remotes: [], identity: undefined, policy: undefined })];
  const merged = g.mergeLightRepos(full, light);
  assert(merged[0].ahead === 2 && merged[0].behind === 1, 'ahead/behind refresh from the light poll');
  assert(merged[0].dirty.staged === 1, 'dirty counts refresh from the light poll');
  assert(deepEqual(merged[0].identity, full[0].identity), 'identity from the last full load survives a light merge untouched');
  assert(deepEqual(merged[0].policy, full[0].policy), 'policy from the last full load survives a light merge untouched');
  assert(merged[0].remotes.length === 1 && merged[0].remotes[0].name === 'origin', 'remotes from the last full load survive even though light omits them');

  const untouched = g.mergeLightRepos(full, []);
  assert(deepEqual(untouched, full), 'a repo the light response does not name is left exactly as it was, not dropped');
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nSource control (lote 85): all checks passed');
