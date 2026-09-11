// B2 (OBJ-8, CONTRATO_OBJ8_B.md) — Studio: the topology adapter (Mermaid
// diagrams, agent-profile/workflow lint, workflow cost estimate — the
// Studio half of `docs/api/topology.md`, Lote A4's backend), a "Lint" panel
// on Defs.tsx, and "Ver diagrama"/"Estimar coste" on a workflow run's
// detail in Activity.tsx. Static source inspection, like
// l86-source-control-panel.check.mjs: this is JSX wiring across several
// large screens, not pure logic a bundled import can exercise on its own.
//
// Run by tests/test_l99_studio_topology_js.py, or by hand:
//   node studio/checks/topology.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

// ── adapters/topology.ts: the five endpoints, correctly shaped ──
{
  const p = 'studio/src/adapters/topology.ts';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes("getJson<{ ok?: boolean; findings?: RawFinding[] }>('/api/agent-profiles/lint'"), 'lintCatalog must call GET /api/agent-profiles/lint');
  assert.ok(src.includes('/api/agent-profiles/lint/${encodeURIComponent(kind)}/${encodeURIComponent(profileId)}'), 'lintProfile must call GET /api/agent-profiles/lint/{kind}/{profile_id}');
  assert.ok(src.includes("postJson<{ ok?: boolean; mermaid?: unknown }>('/api/workflows/mermaid', { definition }"), 'workflowMermaid must POST /api/workflows/mermaid with {definition}');
  assert.ok(src.includes('/api/futures/${encodeURIComponent(futureId)}/mermaid'), 'futureMermaid must call GET /api/futures/{future_id}/mermaid');
  assert.match(src, /postJson<\{ ok\?: boolean; estimate\?: Record<string, unknown> \}>\(\s*'\/api\/workflows\/estimate'/, 'workflowEstimate must POST /api/workflows/estimate');
  for (const exported of ['export async function lintCatalog', 'export async function lintProfile', 'export async function workflowMermaid', 'export async function futureMermaid', 'export async function workflowEstimate', 'export class ProfileLintRefusal']) {
    assert.ok(src.includes(exported), `topology.ts must export: ${exported}`);
  }
  // Every finding field the contract asks the UI to show (code, subject,
  // message, hint) survives the wire shape, not silently dropped.
  for (const field of ['code:', 'severity,', 'subject:', 'message:', 'hint:']) {
    assert.ok(src.includes(field), `LintFinding mapping must keep ${field}`);
  }
}

// ── components/MermaidView.tsx: no new dependency, but genuinely useful
// (copy + .mmd download), reused rather than re-implemented per caller ──
{
  const p = 'studio/src/components/MermaidView.tsx';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes('export function MermaidView'), 'must export MermaidView');
  assert.ok(!/from ['"]mermaid['"]/.test(src), 'must not import the mermaid package — none was added to package.json');
  assert.ok(src.includes('navigator.clipboard.writeText(code)'), 'must offer copy');
  assert.ok(src.includes("new Blob([code]"), 'must offer a .mmd download');
  const pkg = JSON.parse(read('package.json'));
  const deps = { ...(pkg.dependencies ?? {}), ...(pkg.devDependencies ?? {}) };
  assert.ok(!('mermaid' in deps), 'package.json must not gain a mermaid dependency (CONTRATO_OBJ8_B.md: "sin librerías nuevas")');
  const index = read('studio/src/components/index.ts');
  assert.ok(index.includes("export { MermaidView"), 'MermaidView must be exported from components/index.ts');
}

// ── screens/agents/ProfileLint.tsx: findings grouped by severity, with
// code/subject/message/hint, reading the catalogue-wide endpoint ──
{
  const p = 'studio/src/screens/agents/ProfileLint.tsx';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes('export function ProfileLint'), 'must export ProfileLint');
  assert.ok(src.includes("from '../../adapters/topology'") && src.includes('lintCatalog'), 'ProfileLint must read findings through adapters/topology.ts, not a fetch of its own');
  assert.ok(!/\bfetch\(/.test(src), 'ProfileLint must not call fetch() directly — adapters/topology.ts already does');
  for (const field of ['f.code', 'f.subject', 'f.message', 'f.hint']) {
    assert.ok(src.includes(field), `ProfileLint must render ${field}`);
  }
  assert.ok(src.includes('data-severity={f.severity}'), 'each finding row must carry its severity for styling/filtering');
}

// ── screens/agents/Defs.tsx: a "Lint" button that opens ProfileLint ──
{
  const p = 'studio/src/screens/agents/Defs.tsx';
  const src = read(p);
  assert.ok(src.includes("import { ProfileLint } from './ProfileLint';"), 'Defs.tsx must import ProfileLint');
  assert.ok(/label=\{t\('Lint'\)\}/.test(src), 'Defs.tsx must offer a button labelled Lint');
  assert.ok(src.includes('<ProfileLint onClose='), 'Defs.tsx must render <ProfileLint onClose={...} />');
}

// ── screens/Activity.tsx: "Ver diagrama"/"Estimar coste" on a workflow
// run's detail, fetching the run's definition first (the activity list
// itself never carries it) ──
{
  const p = 'studio/src/screens/Activity.tsx';
  const src = read(p);
  assert.ok(src.includes("from '../adapters/topology'") && src.includes('workflowMermaid') && src.includes('workflowEstimate'), 'Activity.tsx must call workflowMermaid/workflowEstimate from adapters/topology.ts');
  assert.ok(src.includes('getWorkflowRunDefinition'), 'Activity.tsx must fetch the run definition before diagramming/estimating it');
  assert.ok(src.includes("import { MermaidView") || /\bMermaidView\b/.test(src), 'Activity.tsx must render the diagram through components/MermaidView');
  assert.ok(/label=\{t\('View diagram'\)\}/.test(src), 'must offer a "View diagram" button');
  assert.ok(/label=\{t\('Estimate cost'\)\}/.test(src), 'must offer an "Estimate cost" button');
  // Both buttons live inside the workflow branch of the detail pane, not
  // the whole screen — a chat/task/render run must not offer them.
  const workflowBlock = src.slice(src.indexOf('{current.workflow && <>'), src.indexOf("{current.kind === 'chat'"));
  assert.ok(workflowBlock.includes('openDiagram(current.id)') && workflowBlock.includes('openEstimate(current.id)'), 'the diagram/estimate buttons must be scoped to the workflow detail branch');
  assert.ok(!/\bfetch\(['"]\/api\/workflows\/(mermaid|estimate)/.test(src), 'Activity.tsx must go through the adapter, not fetch() the topology endpoints directly');
}

// ── No branching-futures screen exists in this tree (grepped before
// writing topology.ts / this check): futureMermaid is kept in the adapter
// for parity with the documented contract, but nothing wires it into a
// screen — there is nothing to wire it into. ──
{
  const { readdirSync, statSync } = await import('node:fs');
  const walk = (dir) => readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : [full];
  });
  const futuresScreens = walk(path('studio/src/screens')).filter((f) => /futures?/i.test(f.split('/').pop()));
  assert.equal(futuresScreens.length, 0, `a futures screen now exists (${futuresScreens.join(', ')}) — wire GET /api/futures/{id}/mermaid into it and update this check`);
}

// ── i18n: every new string this lot introduces has a Spanish row (the
// repo-wide gate is `python3 scripts/i18n_es.py --check`; this is a
// narrower, lot-scoped tripwire so this check fails on its own regression
// even while an unrelated lot's keys are still missing). ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  const mustHave = [
    'Lint', 'Profile and workflow lint', 'View diagram', 'Estimate cost',
    'Workflow diagram', 'Workflow cost estimate', 'Node', 'Calls', 'Cost (USD)',
    'Unbounded loops', 'Download .mmd',
    'This run does not carry its definition — it may predate this build, or have been purged.',
    'The server did not return a diagram.', 'The server did not return a cost estimate.',
  ];
  for (const key of mustHave) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

console.log('ok topology');
