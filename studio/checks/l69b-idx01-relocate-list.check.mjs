// Lote 69b — IDX-01: the "Reubicar" action from the Projects LIST (not just
// from inside one project), and the recent-folders adapter it and
// Project.tsx's Browse fallback both use.
//
// `recentFolders()` (adapters/projects.ts) parses the exact shape
// `routes/project_routes.py::recent_folders` returns
// (`{folders:[{path, project_id, project_name, updated_at}], count}`).
// `relocateResultMessage` stayed importable from BOTH `adapters/projects.ts`
// (its real home now) and `screens/Project.tsx` (a re-export, so the
// existing `l43-project-relocate.check.mjs` — which bundles Project.tsx —
// keeps working unchanged).
//
// Run by tests/test_l69b_studio_checks_js.py, or by hand:
//   node studio/checks/l69b-idx01-relocate-list.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';

async function bundle(entry) {
  const result = await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', write: false, logLevel: 'silent', loader: { '.css': 'empty' }, jsx: 'automatic' });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

let failed = 0;
const check = (condition, message) => {
  if (!condition) { failed += 1; console.error('FAIL:', message); }
  else console.log('ok', message);
};

// ── recentFolders(): the wire shape routes/project_routes.py returns ──
{
  const { recentFolders } = await bundle('studio/src/adapters/projects.ts');
  globalThis.fetch = async (path) => {
    check(String(path).includes('/api/projects/recent-folders'), 'recentFolders() calls GET /api/projects/recent-folders');
    return new Response(JSON.stringify({
      folders: [
        { path: '/home/alex/Projects/acme', project_id: 'p1', project_name: 'Acme', updated_at: 1000 },
        { path: '', project_id: 'p2', project_name: 'Empty path (junk)', updated_at: 900 },
      ],
      count: 2,
    }), { status: 200 });
  };
  const folders = await recentFolders();
  check(folders.length === 1, 'a folder with an empty path is dropped, not shown as a blank suggestion');
  check(folders[0].path === '/home/alex/Projects/acme', 'path passes through');
  check(folders[0].projectName === 'Acme', 'project_name → projectName');
  check(folders[0].projectId === 'p1', 'project_id → projectId');
}

// ── Project.tsx re-exports relocateResultMessage from the adapter (so the
//    existing l43 check, which bundles Project.tsx as its entry point,
//    keeps resolving the same function it always did) ──
{
  const fromProject = await bundle('studio/src/screens/Project.tsx');
  const fromAdapter = await bundle('studio/src/adapters/projects.ts');
  const sample = { project: { id: 'p1', workspace: '/x' }, old_workspace: '/y', old_path_missing: false, marker_conflict: false };
  check(fromProject.relocateResultMessage(sample) === fromAdapter.relocateResultMessage(sample), 'Project.tsx and adapters/projects.ts agree on the exact same message (one re-exports the other)');
}

// ── Projects.tsx (the list) bundles clean and exposes the screen component
//    — proves the new relocate wiring (openRelocate/doRelocate/browseRelocate,
//    the per-row button, the dialog) does not break the module graph. ──
{
  const mod = await bundle('studio/src/screens/Projects.tsx');
  check(typeof mod.ProjectsScreen === 'function', 'ProjectsScreen still exports as a function after adding the relocate dialog');
}

if (failed) {
  console.error(`${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('ALL OK: recentFolders() parses the wire shape, Project.tsx/adapters/projects.ts agree on relocateResultMessage, Projects.tsx bundles clean');
