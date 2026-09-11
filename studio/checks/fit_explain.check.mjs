// CMP-11 (INFORME V2 §3.10) — capabilities -> explainable selection.
// `studio/src/adapters/fit.ts` (fetchFitExplain, the four-state vocabulary),
// `studio/src/screens/ModelPalette.tsx` (a badge per requested capability,
// never a merged yes/no) and `studio/src/shell/palette.css` (state colours,
// tokens only). Static source inspection: the palette is JSX wired to a
// live fetch, not pure logic a bundled import can exercise on its own —
// same shape as topology.check.mjs.
//
// Run by tests/test_cmp11_fit_explain_js.py, or by hand:
//   node studio/checks/fit_explain.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

// ── adapters/fit.ts: fetchFitExplain against the new route, four states,
// never fewer — `unknown` is a state, not an omission ──
{
  const p = 'studio/src/adapters/fit.ts';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes("export type FitExplainState = 'tested' | 'announced' | 'unknown' | 'missing';"),
    'the four CMP-11 states must be named exactly, in this order');
  assert.ok(src.includes('/api/models/fit-explain'), 'fetchFitExplain must call GET /api/models/fit-explain');
  assert.ok(src.includes('export async function fetchFitExplain'), 'must export fetchFitExplain');
  assert.ok(src.includes('export interface FitReason'), 'must export FitReason');
  assert.ok(/capability:\s*string;/.test(src) && src.includes('alternatives: string[];'),
    'FitReason must carry capability, state, message AND alternatives — never drop the WHY or the way out');
  assert.ok(src.includes('export const PICKER_CAPABILITY_NEEDS'), 'must export the picker\'s default needs list');
  assert.ok(/PICKER_CAPABILITY_NEEDS\s*=\s*\[[^\]]*'tools'[^\]]*'json'[^\]]*'vision'[^\]]*'images_edit'[^\]]*\]/.test(src),
    'the default needs must be exactly tools, json, vision, images_edit (INFORME V2 §3.10\'s own list)');
  assert.ok(src.includes("return EMPTY_FIT_EXPLAIN;") || /catch\s*\{[^}]*EMPTY_FIT_EXPLAIN/.test(src),
    'a failed read must leave no badge, never a wrong one — never invent a verdict');
  // fetchModelCapabilities (MOD-01/MOD-02's own, older reader) must survive:
  // other screens (settings/LocalModels.tsx) still depend on it.
  assert.ok(src.includes('export async function fetchModelCapabilities'),
    'fetchModelCapabilities must not be removed — LocalModels.tsx still uses it');
}

// ── screens/ModelPalette.tsx: one badge per requirement, never merged,
// `unknown`/`announced` rendered (not hidden), tooltip carries the cause
// and, for a shortfall, the alternative ──
{
  const p = 'studio/src/screens/ModelPalette.tsx';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes("from '../adapters/fit'") && src.includes('fetchFitExplain'),
    'ModelPalette must read capability fit through adapters/fit.ts, not a fetch of its own');
  assert.ok(!/\bfetch\(/.test(src), 'ModelPalette must not call fetch() directly');
  assert.ok(src.includes('reasons.map((reason)'), 'must render one badge per FitReason, not a merged summary');
  assert.ok(src.includes('data-state={reason.state}'), 'each badge must carry its own state for styling');
  assert.ok(src.includes('reason.message'), 'the tooltip must carry the backend\'s own explanation');
  assert.ok(src.includes('reason.alternatives'), 'the tooltip must offer the alternative when the model falls short');
  // The row must not gate the request on `profile.isLocal` any more — a
  // remote endpoint has SOME evidence too (supports_tools) and honestly
  // reports `unknown` for the rest, so it must be asked as well.
  const requestFn = src.slice(src.indexOf('const requestCaps ='), src.indexOf('const byEndpoint ='));
  assert.ok(!requestFn.includes('profile?.isLocal'), 'requestCaps must not skip remote endpoints — they carry supports_tools evidence too');
}

// ── shell/palette.css: state colours, tokens only (guarded repo-wide by
// tests/test_studio_guards.py; this is the lot-scoped tripwire) ──
{
  const p = 'studio/src/shell/palette.css';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  for (const state of ['tested', 'announced', 'missing']) {
    assert.ok(src.includes(`[data-state='${state}']`), `palette.css must style [data-state='${state}']`);
  }
  assert.ok(!src.includes("[data-ok="), 'the old boolean data-ok styling must be gone, replaced by the four-state one');
  assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(src.match(/\.fs-palette__cap\[data-state[\s\S]*?\}/g)?.join('\n') ?? ''),
    'state colours must come from var(--fs-*) tokens, never a literal hex');
}

// ── i18n: the one new user-facing string this lot introduces ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  assert.ok(keys.has('Alternatives: {names}'), 'docs/ui/i18n/es.tsv is missing a Spanish row for: Alternatives: {names}');
}

// ── backend counterpart exists and is wired at the route this file's
// fetch calls — a narrow tripwire, not a substitute for the Python tests ──
{
  const src = read('routes/model_routes.py');
  assert.ok(src.includes('@router.get("/models/fit-explain")'), 'routes/model_routes.py must expose GET /models/fit-explain');
  assert.ok(src.includes('mc.explain_fit('), 'the route must delegate to src.model_capabilities.explain_fit');
}
{
  const src = read('src/model_capabilities.py');
  for (const name of ['def explain_fit', 'class Fit', 'class FitReason', 'class FitCandidateModel',
    'FIT_TESTED', 'FIT_ANNOUNCED', 'FIT_UNKNOWN', 'FIT_MISSING']) {
    assert.ok(src.includes(name), `src/model_capabilities.py must define ${name}`);
  }
}

console.log('ok fit_explain');
