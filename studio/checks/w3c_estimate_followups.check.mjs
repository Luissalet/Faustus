// W3-C (CMP-08 follow-up, CONTRATO_W3.md) — the separated-accounts /
// "previsto vs real" / "Comparar planes" UI in
// studio/src/screens/activity/EstimateView.tsx, and the owner/projectId
// pickup in studio/src/adapters/topology.ts. Static source inspection, the
// same pattern topology.check.mjs already uses for this same tree: this is
// JSX wiring plus a small adapter surface, not pure logic a bundled import
// can exercise standalone.
//
// Run by tests/test_w3c_estimate_followups.py, or by hand:
//   node studio/checks/w3c_estimate_followups.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

// ── screens/activity/EstimateView.tsx: optional definition/runId props,
// backward-compatible with Activity.tsx's existing `estimate={estimateResult}`
// call site (this lot does not own Activity.tsx and must not require it to
// change) — plus the detailed accounts, the unknown-is-never-zero rendering,
// and the "Comparar planes" wiring. ──
{
  const p = 'studio/src/screens/activity/EstimateView.tsx';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes('export function WorkflowEstimateView'), 'must still export WorkflowEstimateView');
  assert.ok(/definition\?:\s*Record<string, unknown>/.test(src), 'definition must stay an OPTIONAL prop — Activity.tsx does not pass it yet');
  assert.ok(/runId\?:\s*string/.test(src), 'runId must stay an OPTIONAL prop');
  assert.ok(src.includes('estimate: WorkflowEstimate'), 'the original `estimate` prop must still be required — no breaking change for the existing caller');

  assert.ok(src.includes("from '../../adapters/topology'") && src.includes('workflowEstimateDetailed') && src.includes('comparePlans'),
    'must fetch through adapters/topology.ts, not fetch() directly');
  assert.ok(!/\bfetch\(/.test(src), 'EstimateView.tsx must not call fetch() directly');

  // Separated accounts: node activations, model calls and external ops are
  // rendered as genuinely different rows, never folded into one number.
  for (const field of ['detail.nodeActivations', 'detail.modelCalls', 'detail.externalOps', 'detail.tokens.in', 'detail.tokens.out', 'detail.costKnownUsd']) {
    assert.ok(src.includes(field), `must render the separated account: ${field}`);
  }
  assert.ok(src.includes('detail.assumptions'), 'must list the assumptions used, not hide them in the total');
  assert.ok(src.includes('detail.costUnestimable'), 'must list every reason a total is incomplete');

  // unknown is never shown as 0 or free.
  assert.ok(src.includes('function fmtMaybeUnknown'), 'must have a helper that renders unknown/null/undefined distinctly from a number');
  assert.ok(/callsProfileSource === 'unknown'/.test(src), 'a node with an unknown calls_profile must render its model calls/tokens as unknown, not 0');

  // Previsto vs real.
  assert.ok(src.includes('detail.measured') && src.includes("t('Forecast vs actual')"), 'must show forecast vs actual when a run has measured usage');

  // Comparar planes: builds single_model + deterministic_steps variants and
  // calls the real compare-plans endpoint through the adapter.
  assert.ok(src.includes('function buildComparisonPlans'), 'must build the comparison plans');
  assert.ok(src.includes("id: 'single_model'") && src.includes("id: 'deterministic_steps'") && src.includes("id: 'current'"),
    'must compare the current plan against a single-model and a deterministic-steps variant');
  assert.ok(src.includes('comparePlans('), 'must call the adapter\'s comparePlans');
  assert.ok(/label=\{t\('Compare plans'\)\}/.test(src), 'must offer a "Compare plans" button');
  assert.ok(src.includes("data-testid=\"estimate-compare-plans\"") || src.includes("testId=\"estimate-compare-plans\""), 'the Compare plans button must carry a stable test id');
  assert.ok(src.includes('data-basis={cell.basis}'), 'the comparison table must tag each cell computed/estimated/unknown');
}

// ── activity.css: only .fs-estimate* rules were touched (this lot's
// exclusive slice of the file) — a cheap tripwire against scope creep. ──
{
  const css = read('studio/src/screens/activity.css');
  assert.ok(css.includes('.fs-estimate__detail'), 'must add the detailed-estimate section styling');
  assert.ok(css.includes(".fs-estimate__basis[data-basis='computed']") && css.includes(".fs-estimate__basis[data-basis='unknown']"),
    'must style the computed/estimated/unknown basis tags');
  assert.ok(!/\.fs-estimate__basis\s*\{[^}]*#[0-9a-fA-F]{3,6}/.test(css), 'no literal colors — tokens only (var(--fs-*))');
}

// ── adapters/topology.ts: owner/projectId threaded through for the
// manifest-declared calls_profile pickup (routes/workflows_routes.py). ──
{
  const src = read('studio/src/adapters/topology.ts');
  assert.ok(/owner\?:\s*string;/.test(src), 'DetailedEstimateOptions must gain an optional owner field');
  assert.ok(/projectId\?:\s*string;/.test(src), 'DetailedEstimateOptions must gain an optional projectId field');
  assert.ok(src.includes('body.owner = options.owner') && src.includes('body.project_id = options.projectId'),
    'detailBody must thread owner/project_id onto the request body');
}

// ── i18n: every new string this lot introduces has a Spanish row. ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  const mustHave = [
    'load', 'gen', 'queue', 'computed', 'estimated', 'Metric', 'Current plan',
    'Single model', 'Deterministic steps', 'Compare plans', 'Activations',
    'External ops', 'Tokens in/out', 'Local latency', 'Tokens in', 'Tokens out',
    'Known cost (USD)', 'Assumptions used', 'Per-node breakdown', 'Forecast vs actual',
    'Model calls', 'External operations', 'Node activations', '{n} tok/s',
    'Cost of the current workflow', 'Loading the detailed estimate…', 'Separate accounts',
    'Unknown — never shown as zero or free',
  ];
  for (const key of mustHave) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

console.log('ok w3c_estimate_followups');
