// W2-E (CMP-07, CONTRATO_CMP_W2.md) — Studio /workflows: design a plan,
// walk it structurally with adapters/topology.ts's workflowSimulate
// (nothing executed on the server), then authorize a real run over the
// same definition. Static source inspection, like
// l99-studio-topology.check.mjs and l86-source-control-panel.check.mjs:
// this is JSX wiring across several screens, not pure logic a bundled
// import alone can exercise.
//
// Run by tests/test_cmp07_workflows_js.py, or by hand:
//   node studio/checks/workflows.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

// ── adapters/topology.ts: the four endpoints this lot adds ──
{
  const p = 'studio/src/adapters/topology.ts';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes("postJson<{ ok?: boolean; simulation?: Record<string, unknown> }>('/api/workflows/simulate'"), 'workflowSimulate must POST /api/workflows/simulate');
  assert.ok(src.includes("postJson<{ ok?: boolean; preflight?: Record<string, unknown> }>('/api/workflows/preflight'"), 'workflowPreflight must POST /api/workflows/preflight');
  assert.ok(src.includes("postJson<Record<string, unknown>>('/api/workflows/import'"), 'importWorkflowDefinition must POST /api/workflows/import');
  assert.ok(src.includes("postJson<{ ok?: boolean; export?: Record<string, unknown> }>('/api/workflows/export'"), 'exportWorkflowDefinition must POST /api/workflows/export');
  for (const exported of ['export async function workflowSimulate', 'export async function workflowPreflight', 'export async function importWorkflowDefinition', 'export async function exportWorkflowDefinition']) {
    assert.ok(src.includes(exported), `topology.ts must export: ${exported}`);
  }
  // A node with no choices entry must come back undecided, never guessed
  // past — the wire field names that carry that.
  for (const field of ['blockedBy:', 'awaitingChoice', 'notReached', 'humanWaits', 'roundsMax']) {
    assert.ok(src.includes(field), `simulation mapping must keep ${field}`);
  }
}

// ── screens/workflows/PlanGraph.tsx: layered SVG, guard-exempt, keyboard
// navigable, with a text alternative ──
{
  const p = 'studio/src/screens/workflows/PlanGraph.tsx';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes('export function PlanGraph'), 'must export PlanGraph');
  assert.ok(/<svg[^>]*data-note="guard-ok:/.test(src), 'the <svg> tag must carry a guard-ok exemption comment (no inline-svg guard, ADP-14/UI-012)');
  assert.ok(src.includes('role="button"') && src.includes('tabIndex={0}') && src.includes('onKeyDown'), 'each node must be keyboard-reachable and activatable');
  assert.ok(src.includes('ArrowRight') && src.includes('ArrowLeft') && src.includes('ArrowUp') && src.includes('ArrowDown'), 'must support arrow-key navigation between layers');
  assert.ok(src.includes('<details') && src.includes('<table'), 'must offer a readable list/table alternative to the SVG (accessibility)');
  assert.ok(!/from ['"]reactflow['"]|from ['"]react-flow/.test(src), 'no graph-drawing dependency (ADP-14 limit: no react-flow, no new library)');
}

// ── screens/workflows/NodeInspector.tsx: contract + config + lint ──
{
  const p = 'studio/src/screens/workflows/NodeInspector.tsx';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes('export function NodeInspector'), 'must export NodeInspector');
  assert.ok(src.includes('node-inspector-config'), 'must render the editable config field');
  assert.ok(src.includes('onApplyConfig'), 'applying a config change must be delegated to the caller (which re-runs preflight)');
  assert.ok(src.includes('node-inspector-lint'), 'must render lint findings scoped to this node');
}

// ── screens/workflows/RunOverlay.tsx: the same graph, a real run's state
// superimposed, a confirmed start ──
{
  const p = 'studio/src/screens/workflows/RunOverlay.tsx';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes('export function RunOverlay'), 'must export RunOverlay');
  assert.ok(src.includes("from './PlanGraph'") && src.includes('<PlanGraph'), 'must reuse PlanGraph, not draw a second graph');
  assert.ok(src.includes('Dialog') && src.includes('confirming'), 'starting a real run must go through an explicit confirmation, never fire on the first click');
  assert.ok(src.includes("/api/workflows/runs") && src.includes("method: 'POST'"), 'must call the existing POST /api/workflows/runs to start a real run');
  assert.ok(src.includes('changeWorkflow'), "must drive an existing run through adapters/activity.ts's changeWorkflow, not a second implementation");
}

// ── screens/workflows/WorkflowsScreen.tsx: three explicit modes, one
// shared `definition`, lint findings that open the right node ──
{
  const p = 'studio/src/screens/workflows/WorkflowsScreen.tsx';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes('export function WorkflowsScreen'), 'must export WorkflowsScreen');
  assert.ok(!/\bfetch\(/.test(src), 'WorkflowsScreen must call the adapter, not fetch() directly');
  for (const mode of ["'design'", "'simulate'", "'execute'"]) {
    assert.ok(src.includes(mode), `must declare the ${mode} mode`);
  }
  assert.ok(src.includes('data-testid={`workflows-mode-${m}`}'), 'the three modes must each carry their own testid');
  assert.ok(src.includes('openNodeFromFindingSubject') && src.includes('setSelectedNodeId'), 'a lint finding must be able to open the node it is about');
  assert.ok(src.includes('importWorkflowDefinition') && src.includes('workflows-paste'), 'must offer paste/import of a definition');
  assert.ok(src.includes('loadActivity') && src.includes("r.kind === 'workflow'"), 'recent definitions must come from activity.ts, not a second runs listing');
  // The SAME `definition` object flows into every mode — no per-mode copy
  // that could silently drift into a second format.
  assert.ok(src.includes('<PlanGraph nodes={nodes}') && src.includes('definition={definition}'), 'Design/Simulate and Execute must draw from the one definition in state');
}

// ── routing: /workflows registered everywhere a route must be ──
{
  const routes = read('studio/src/shell/routes.ts');
  assert.ok(/\{ path: '\/workflows', label: 'Workflows', icon: \w+ \},/.test(routes), 'routes.ts TOOLS must list /workflows');
  assert.ok(/^\s*'\/workflows',\s*$/m.test(routes), 'routes.ts SERVER_ROUTES must list /workflows');

  const shell = read('studio/src/shell/AppShell.tsx');
  assert.ok(shell.includes("const WorkflowsScreen = lazy(() => import('../screens/workflows/WorkflowsScreen')"), 'AppShell must lazily import WorkflowsScreen');
  assert.ok(shell.includes('<Route path="/workflows" element={<WorkflowsScreen />} />'), 'AppShell must register the /workflows route');

  const appPy = read('app.py');
  assert.ok(/@app\.get\("\/workflows"\)/.test(appPy), 'app.py must serve GET /workflows (deep links must not 404)');
}

// ── i18n: every new string this lot introduces has a Spanish row ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  const mustHave = [
    'Workflows', 'Design', 'Structural simulation', 'Authorized real execution',
    'Recent runs', 'Import or paste JSON', 'Run simulation', 'Rounds max',
    'Assume passes/approved', 'Assume fails/denied', 'Undecided',
    'Start a real run?', 'Start for real', 'Human waits',
    'Select a node in the plan to inspect its contract.',
    'Lint findings for this node', 'View as a list (keyboard/screen-reader alternative)',
    'The server did not return a simulation.', 'The server did not return a preflight report.',
    'The server did not return an export.',
  ];
  for (const key of mustHave) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

console.log('ok workflows');
