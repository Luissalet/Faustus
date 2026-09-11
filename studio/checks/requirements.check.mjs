// W4-A — Studio: the Requirements tab (adapters/requirements.ts, screens/
// project/Requirements.tsx, wired into Project.tsx next to Board/
// Objectives). Backend: `src/requirements/{store,context,evidence}.py`,
// `routes/requirements_routes.py`, `docs/api/requirements.md`,
// `docs/requirements-format.md` (all built by a previous wave; this lot is
// the screen only).
//
// Two halves: a bundled, functional exercise of the adapter's request
// shaping and pure helpers (mocked `fetch`, no network — same pattern
// `approval-errors.check.mjs` uses for `adapters/chat.ts`), then static
// source inspection of the screen and its registration, like
// `topology.check.mjs` / `alternatives.check.mjs` — this is JSX wiring
// across a large screen, not pure logic a bundled import alone can exercise.
//
// Run by tests/test_w4a_requirements_js.py, or by hand:
//   node studio/checks/requirements.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

// ── the files this lot owns exist ──
for (const p of [
  'studio/src/adapters/requirements.ts',
  'studio/src/screens/project/Requirements.tsx',
]) {
  assert.ok(existsSync(path(p)), `missing ${p}`);
}

// ── functional: adapters/requirements.ts, bundled and run against a mocked
// fetch — request shaping (method, URL, body) and the pure helpers. ──
{
  const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
  const out = join(mkdtempSync(join(tmpdir(), 'fs-requirements-')), 'requirements.mjs');
  await build({
    entryPoints: [join(root, 'studio/src/adapters/requirements.ts')],
    bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent',
  });
  const mod = await import(pathToFileURL(out).href);

  // listRequirements: filters become a query string, GET, no body.
  {
    let seen = null;
    globalThis.fetch = async (url, init) => {
      seen = { url, init };
      return new Response(JSON.stringify({ requirements: [{ key: 'REQ-1' }] }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    };
    const data = await mod.listRequirements('p1', { status: 'proposed', q: 'login' });
    assert.equal(seen.url, '/api/projects/p1/requirements?status=proposed&q=login');
    assert.equal(seen.init.method, undefined, 'a GET must not set method/body');
    assert.equal(data.requirements[0].key, 'REQ-1');
  }

  // createRequirement: POST with the exact body given, JSON content type.
  {
    let seen = null;
    globalThis.fetch = async (url, init) => {
      seen = { url, init, body: JSON.parse(init.body) };
      return new Response(JSON.stringify({ requirement: { key: 'REQ-2' } }), { status: 201, headers: { 'Content-Type': 'application/json' } });
    };
    await mod.createRequirement('p1', { title: 'Users can log in', acceptance: ['a', 'b'], proposed_by: 'human' });
    assert.equal(seen.url, '/api/projects/p1/requirements');
    assert.equal(seen.init.method, 'POST');
    assert.equal(seen.init.headers['Content-Type'], 'application/json');
    assert.deepEqual(seen.body.acceptance, ['a', 'b']);
  }

  // updateRequirement/acceptRequirement/rejectRequirement: ALWAYS `by:
  // 'human'` on the wire, even if a caller's patch object tried to smuggle
  // a `by` of its own -- this is the one rule this adapter enforces itself
  // (ADP-18: only a human moves status to accepted/rejected).
  {
    let seen = null;
    globalThis.fetch = async (url, init) => {
      seen = { url, init, body: JSON.parse(init.body) };
      return new Response(JSON.stringify({ requirement: { key: 'REQ-1', status: 'accepted' } }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    };
    await mod.acceptRequirement('p1', 'REQ-1', 'looks right');
    assert.equal(seen.url, '/api/projects/p1/requirements/REQ-1');
    assert.equal(seen.init.method, 'PATCH');
    assert.equal(seen.body.by, 'human');
    assert.equal(seen.body.status, 'accepted');
    assert.equal(seen.body.change_note, 'looks right');

    await mod.updateRequirement('p1', 'REQ-1', { title: 'x', by: 'model' });
    assert.equal(seen.body.by, 'human', 'a smuggled by:"model" on the patch is overridden, never sent through');
  }

  // addLink: POST to .../links with kind/target/revision.
  {
    let seen = null;
    globalThis.fetch = async (url, init) => {
      seen = { url, init, body: JSON.parse(init.body) };
      return new Response(JSON.stringify({ link: { id: 'lnk_1', kind: 'implements' } }), { status: 201, headers: { 'Content-Type': 'application/json' } });
    };
    await mod.addLink('p1', 'REQ-1', { kind: 'implements', target: 'src/auth.py@login' });
    assert.equal(seen.url, '/api/projects/p1/requirements/REQ-1/links');
    assert.equal(seen.body.kind, 'implements');
    assert.equal(seen.body.target, 'src/auth.py@login');
  }

  // A non-ok response surfaces RequirementsApiError with the server's
  // error_class and detail intact -- the exact class the UI special-cases
  // for a rejected out-of-workspace link.
  {
    globalThis.fetch = async () => new Response(
      JSON.stringify({ error_class: 'requirements.path_outside_workspace', detail: 'outside the workspace' }),
      { status: 409, headers: { 'Content-Type': 'application/json' } },
    );
    await assert.rejects(
      () => mod.addLink('p1', 'REQ-1', { kind: 'implements', target: '../../etc/passwd' }),
      (err) => {
        assert.ok(err instanceof mod.RequirementsApiError);
        assert.equal(err.errorClass, 'requirements.path_outside_workspace');
        assert.equal(err.status, 409);
        assert.equal(err.message, 'outside the workspace');
        return true;
      },
    );
  }

  // contextForTask: POST /context with files/keys/budget_chars.
  {
    let seen = null;
    globalThis.fetch = async (url, init) => {
      seen = { url, init, body: JSON.parse(init.body) };
      return new Response(
        JSON.stringify({ project_id: 'p1', requirements: [], omitted: [], unknown: ['REQ-9'], budget_chars: 500, used_chars: 0 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    };
    const result = await mod.contextForTask('p1', { files: ['src/a.py'], keys: ['REQ-9'], budget_chars: 500 });
    assert.equal(seen.url, '/api/projects/p1/requirements/context');
    assert.deepEqual(seen.body, { files: ['src/a.py'], keys: ['REQ-9'], budget_chars: 500 });
    assert.deepEqual(result.unknown, ['REQ-9'], 'an unknown requested id is reported, never invented');
  }

  // Pure helpers -- no fetch involved.
  assert.deepEqual(mod.acceptanceFromLines('a\n b \n\nc'), ['a', 'b', 'c'], 'blank lines dropped, each line trimmed');
  assert.equal(mod.acceptanceToLines(['a', 'b']), 'a\nb');
  assert.equal(mod.humanOnlyStatus('accepted'), true);
  assert.equal(mod.humanOnlyStatus('rejected'), true);
  assert.equal(mod.humanOnlyStatus('proposed'), false);
  assert.equal(mod.humanOnlyStatus('superseded'), false);
  for (const status of ['proposed', 'accepted', 'rejected', 'superseded']) {
    assert.ok(['ok', 'bad', 'warn', 'neutral'].includes(mod.STATUS_TONE[status]), `STATUS_TONE must cover ${status}`);
  }
  for (const state of ['linked', 'needs_review', 'stale', 'unknown']) {
    assert.ok(['ok', 'bad', 'warn', 'neutral'].includes(mod.LINK_STATE_TONE[state]), `LINK_STATE_TONE must cover ${state}`);
  }
  assert.deepEqual(mod.LINK_KINDS, ['implements', 'tests', 'evidences', 'issue']);
  assert.deepEqual(mod.REQUIREMENT_STATUSES, ['proposed', 'accepted', 'rejected', 'superseded']);
}

// ── screens/project/Requirements.tsx: reads through the adapter only,
// never fetch() directly ──
{
  const src = read('studio/src/screens/project/Requirements.tsx');
  assert.ok(src.includes('export function ProjectRequirements'), 'must export ProjectRequirements');
  assert.ok(!/\bfetch\(/.test(src), 'Requirements.tsx must call the adapter, not fetch() directly');
  assert.ok(src.includes("from '../../adapters/requirements'"), 'must read through adapters/requirements.ts');

  // Human-only accept/reject: the screen never calls updateRequirement with
  // a status change directly for that decision -- it goes through the two
  // dedicated helpers, and never imports/uses a `by: 'model'` literal.
  assert.ok(src.includes('acceptRequirement') && src.includes('rejectRequirement'), 'must offer accept/reject through the dedicated adapter calls');
  assert.ok(!/by:\s*'model'/.test(src) && !/by:\s*"model"/.test(src), "the screen must never send by: 'model' -- accept/reject here is always a human");
  assert.ok(src.includes("canDecide = item.status === 'proposed'"), 'accept/reject controls must be gated on status === proposed');

  // Evidence matrix: four independent facts plus stale, both per-requirement
  // and project-wide (GET /matrix).
  for (const fn of ['getRequirementMatrix', 'getProjectMatrix']) {
    assert.ok(src.includes(fn), `must call ${fn}`);
  }
  assert.ok(src.includes('row.linked') && src.includes('row.implemented') && src.includes('row.tested') && src.includes('row.verified') && src.includes('row.stale'),
    'the matrix view must render all four independent facts plus stale, never a folded score');

  // Revisions are read-only history, never edited.
  assert.ok(src.includes('getRevisions'), 'must offer the immutable revision history');

  // Links: add and withdraw. The remove call must hit the scoped DELETE
  // route (`/{key}/links/{link_id}`), never a bare `/links/{id}` that could
  // reach another requirement's link.
  assert.ok(src.includes('addLink'), 'must offer adding a link');
  assert.ok(src.includes('removeLink'), 'must offer withdrawing a link');
  const adapterSrc = read('studio/src/adapters/requirements.ts');
  assert.ok(/method:\s*'DELETE'/.test(adapterSrc), 'removeLink must use DELETE');
  assert.ok(/\/links\/\$\{encodeURIComponent\(linkId\)\}/.test(adapterSrc), 'removeLink must be scoped under the requirement key');

  // Context-for-task tool surfaces omitted/unknown explicitly.
  assert.ok(src.includes('contextForTask'), 'must call the context-for-task endpoint');
  assert.ok(src.includes('result.omitted') && src.includes('result.unknown'), 'omitted/unknown must be rendered explicitly, never silently dropped');

  // Sidecar preview: only shown when the file actually exists, never
  // auto-imported into the database.
  assert.ok(src.includes('getSidecar'), 'must read the sidecar preview');
  assert.ok(src.includes('if (!sidecar || !sidecar.path) return null'), 'the sidecar section must hide itself when no sidecar file exists');
  assert.ok(src.includes('createRequirement'), 'a sidecar item becomes a real requirement only through an explicit, per-item human action');

  // Empty states use the shared component, not an ad hoc paragraph, for the
  // two genuine "nothing/nobody selected" cases.
  assert.ok((src.match(/<EmptyState/g) ?? []).length >= 2, 'must use EmptyState for at least the empty list and the no-selection state');
}

// ── Project.tsx: registered as a real tab next to Board/Objectives,
// following the exact `?tab=` pattern those already use ──
{
  const src = read('studio/src/screens/Project.tsx');
  assert.ok(src.includes("import { ProjectRequirements } from './project/Requirements';"), 'Project.tsx must import ProjectRequirements');
  assert.ok(/\{\s*id:\s*'requisitos',\s*label:\s*'Requirements'/.test(src), 'TABS must list a requisitos/Requirements entry');
  assert.ok(src.includes("tab === 'requisitos'") && src.includes('<ProjectRequirements projectId={project.id} say={say} />'), 'the requisitos tab must render <ProjectRequirements>');
  // Registered the same way as the neighbouring tabs -- same TABS array,
  // same ?tab= state, not a second routing mechanism.
  const tabsBlock = src.slice(src.indexOf('const TABS = ['), src.indexOf('] as const;'));
  assert.ok(tabsBlock.includes("id: 'board'") && tabsBlock.includes("id: 'objetivos'") && tabsBlock.includes("id: 'requisitos'"),
    'requisitos must live in the same TABS array as board/objetivos, not a separate mechanism');
}

// ── i18n: every new string this lot introduces has a Spanish row ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  const mustHave = [
    'Requirements', 'New requirement', 'Add requirement', 'Accept', 'Reject',
    'Context for a task', 'Coverage matrix — whole project', 'Revisions',
    'Sidecar: .faustus/requirements.yaml', 'Create as requirement',
    'Human', 'Proposed by a human', 'Proposed by the model',
    'Omitted (did not fit the budget)', 'Unknown ids',
    'No requirements yet', 'No requirement selected', 'No links yet.',
  ];
  for (const key of mustHave) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

console.log('ok requirements');
