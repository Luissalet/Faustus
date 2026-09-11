// W2-G (CMP-13, CONTRATO_CMP_W2.md) — Studio: the Alternatives screen
// (isolated experiments, compare, apply/combine with a conflict banner),
// wired into the router/sidebar the same way `/source-control` already is.
// Static source inspection, like topology.check.mjs / l86-source-control-
// panel.check.mjs: this is JSX wiring across several screens, not pure logic
// a bundled import can exercise on its own.
//
// Run by tests/test_cmp13_alternatives_js.py, or by hand:
//   node studio/checks/alternatives.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

// ── the files this lot owns exist ──
for (const p of [
  'studio/src/adapters/alternatives.ts',
  'studio/src/screens/alternatives/AlternativesScreen.tsx',
  'studio/src/screens/alternatives/CompareView.tsx',
  'studio/src/screens/alternatives/alternatives.css',
]) {
  assert.ok(existsSync(path(p)), `missing ${p}`);
}

// ── adapters/alternatives.ts: a real error class carrying `conflicts`,
// no direct fetch anywhere else, every endpoint correctly shaped ──
{
  const src = read('studio/src/adapters/alternatives.ts');
  assert.ok(src.includes('export class AlternativesApiError extends ApiError'), 'must export AlternativesApiError extending ApiError');
  assert.ok(src.includes("errorClass === 'alternatives.apply_conflict'") || src.includes('conflicts: Array.isArray'), 'must surface conflicts from the payload');
  for (const fn of [
    'export function listExperiments', 'export function createExperiment', 'export function getExperiment',
    'export function deleteExperiment', 'export function compareExperiment', 'export function addAlternative',
    'export function setDocContent', 'export function runTests', 'export function applyAlternative',
    'export function combine',
  ]) {
    assert.ok(src.includes(fn), `alternatives.ts must export: ${fn}`);
  }
  assert.ok(src.includes('/api/projects/'), 'must call the project-scoped alternatives API');
  assert.ok(!/\bconsole\.log\(/.test(src), 'no stray console.log');
}

// ── screens: read through the adapter only, never fetch() directly ──
for (const p of ['studio/src/screens/alternatives/AlternativesScreen.tsx', 'studio/src/screens/alternatives/CompareView.tsx']) {
  const src = read(p);
  assert.ok(!/\bfetch\(/.test(src), `${p} must call the adapter, not fetch() directly`);
}

// ── AlternativesScreen.tsx: lists experiments, offers a new one, opens
// CompareView, and the conflict/contested banners are wired through it ──
{
  const src = read('studio/src/screens/alternatives/AlternativesScreen.tsx');
  assert.ok(src.includes('export function AlternativesScreen'), 'must export AlternativesScreen');
  assert.ok(src.includes("from '../../adapters/alternatives'"), 'must read through adapters/alternatives.ts');
  assert.ok(src.includes("import { CompareView } from './CompareView'"), 'must render CompareView for an open experiment');
  assert.ok(src.includes("useSearchParams"), 'the open experiment must be a real, bookmarkable URL (?exp=), not local-only state');
  assert.ok(src.includes('listProjects'), 'must offer a real project picker, not a free-text project id');
}

// ── CompareView.tsx: diffs, tests, apply -- and the conflict banner never
// silently swallows a conflict into a toast ──
{
  const src = read('studio/src/screens/alternatives/CompareView.tsx');
  assert.ok(src.includes('export function CompareView'), 'must export CompareView');
  assert.ok(src.includes('compareExperiment'), 'must call compareExperiment');
  assert.ok(src.includes('applyAlternative'), 'must call applyAlternative');
  assert.ok(src.includes("errorClass === 'alternatives.apply_conflict'"), 'must special-case the conflict error class');
  assert.ok(src.includes('setConflicts'), 'a conflict must become its own state, not a generic toast message');
  assert.ok(src.includes('contested_files') || src.includes('contested'), 'must surface files more than one alternative touches');
  assert.ok(src.includes('runTests') || src.includes('onRunTests'), 'must offer running a command inside an alternative');
  assert.ok(/costLabel|known_usd/.test(src), 'must render the cost, unknown included -- never silently dropped');
}

// ── routing: server whitelist, client route table, sidebar/palette, and
// the lazy Route entry all agree on /alternatives ──
{
  const app = read('app.py');
  assert.ok(/@app\.get\("\/alternatives"\)/.test(app), 'app.py must serve GET /alternatives as a deep link');
  assert.ok(app.includes('from routes.alternatives_routes import setup_alternatives_routes'), 'app.py must import setup_alternatives_routes');
  assert.ok(app.includes('app.include_router(setup_alternatives_routes())'), 'app.py must mount the alternatives router');
}
{
  const routes = read('studio/src/shell/routes.ts');
  assert.ok(/\{\s*path:\s*'\/alternatives'/.test(routes), 'routes.ts TOOLS must list /alternatives');
  assert.ok(routes.includes("'/alternatives'") && routes.includes('SERVER_ROUTES'), 'routes.ts SERVER_ROUTES must list /alternatives, kept in step with app.py');
}
{
  const shell = read('studio/src/shell/AppShell.tsx');
  assert.ok(shell.includes("import('../screens/alternatives/AlternativesScreen')"), 'AppShell.tsx must lazy-import AlternativesScreen');
  assert.ok(/<Route path="\/alternatives" element=\{<AlternativesScreen \/>\}\s*\/>/.test(shell), 'AppShell.tsx must route /alternatives to AlternativesScreen');
}

// ── backend registration this lot owns: worktree helpers on git_panel,
// the alt_* agent tools registered everywhere a tool must be (mirrors
// git_tools' own six-file registration, per the contract) ──
{
  const gitPanel = read('src/git_panel.py');
  for (const fn of ['def worktree_add', 'def worktree_remove', 'def worktree_list']) {
    assert.ok(gitPanel.includes(fn), `git_panel.py must define ${fn}`);
  }
}
{
  const init = read('src/agent_tools/__init__.py');
  assert.ok(init.includes('from .alternatives_tools import'), '__init__.py must import the alternatives tools');
  for (const name of ['"alt_start"', '"alt_compare"', '"alt_apply"']) {
    assert.ok(init.includes(`${name}:`), `__init__.py TOOL_HANDLERS must register ${name}`);
  }
}
{
  const schemas = read('src/tool_schemas.py');
  for (const name of ['"name": "alt_start"', '"name": "alt_compare"', '"name": "alt_apply"']) {
    assert.ok(schemas.includes(name), `tool_schemas.py must define ${name}`);
  }
}
{
  const caps = read('src/tool_capabilities.py');
  assert.ok(/"alt_start"/.test(caps) && /"alt_compare"/.test(caps) && /"alt_apply"/.test(caps), 'tool_capabilities.py must classify the alt_* tools');
}
{
  const index = read('src/tool_index.py');
  assert.ok(index.includes('"alt_start":') && index.includes('"alt_compare":') && index.includes('"alt_apply":'), 'tool_index.py BUILTIN_TOOL_DESCRIPTIONS must describe the alt_* tools');
}
{
  const examples = read('src/tool_index_examples.py');
  assert.ok(examples.includes('"alt_start"') && examples.includes('"alt_compare"') && examples.includes('"alt_apply"'), 'tool_index_examples.py must give examples for the alt_* tools');
}
{
  const security = read('src/tool_security.py');
  assert.ok(security.includes('"alt_start"') && security.includes('"alt_compare"') && security.includes('"alt_apply"'), 'tool_security.py NON_ADMIN_BLOCKED_TOOLS must include the alt_* tools -- same privilege class as git_*');
}

// ── i18n: every new string this lot introduces has a Spanish row ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  const mustHave = [
    'Alternatives', 'New experiment', 'Add alternative', 'No experiments yet',
    'This apply needs a human: these files changed on both sides since the experiment started, and nothing was written.',
    'Nothing to apply: the main copy already matches.', '{n} alternative(s)',
  ];
  for (const key of mustHave) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

console.log('ok alternatives');
