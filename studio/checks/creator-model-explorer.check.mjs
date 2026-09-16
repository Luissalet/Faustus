// WP08 — Model Explorer y comparación: studio/src/adapters/creator_models.ts,
// studio/src/screens/creator/ModelExplorer.tsx, wired as a tab into
// CreatorScreen.tsx (SOLO add), over routes/creator_model_explorer_routes.py.
//
// Static source inspection for the wiring (like creator-shell.check.mjs),
// plus a real esbuild bundle of the adapter to exercise response decoding
// against a fake fetch, so this proves behavior, not just that the source
// text mentions the right names.
//
// Run by hand:
//   node studio/checks/creator-model-explorer.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import { execFileSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');
const { build: esbuildBuild } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);

// ── the files this lot owns exist ──
for (const p of [
  'studio/src/adapters/creator_models.ts',
  'studio/src/screens/creator/ModelExplorer.tsx',
  'src/creator/model_explorer.py',
  'routes/creator_model_explorer_routes.py',
]) {
  assert.ok(existsSync(path(p)), `missing ${p}`);
}

// ── adapters/creator_models.ts: every route it must cover ──
{
  const src = read('studio/src/adapters/creator_models.ts');
  for (const fn of [
    'export function listExplorer', 'export function getExplorerEntry', 'export function compareDeployments',
  ]) {
    assert.ok(src.includes(fn), `creator_models.ts must export: ${fn}`);
  }
  for (const route of ['/api/creator/models/explorer', '/api/creator/models/compare']) {
    assert.ok(src.includes(route), `creator_models.ts must call ${route}`);
  }
  assert.ok(src.includes('CompareBasis'), 'must type the basis vocabulary (known/announced/unsupported/unknown/measured/estimated)');
  assert.ok(!/\bconsole\.log\(/.test(src), 'no stray console.log');
}

// ── ModelExplorer.tsx: reads through the adapter only, never fetch() directly ──
{
  const src = read('studio/src/screens/creator/ModelExplorer.tsx');
  assert.ok(!/\bfetch\(/.test(src), 'ModelExplorer.tsx must call the adapter, not fetch() directly');
  assert.ok(src.includes('export function ModelExplorer'), 'must export ModelExplorer');
  assert.ok(src.includes("from '../../adapters/creator_models'"), 'must read through adapters/creator_models.ts');
  assert.ok(src.includes('putCreatorProfile'), '"Usar para esta tarea" must save through the existing profile adapter (WP02), not a new endpoint');
  assert.ok(src.includes('BasisBadge') || src.includes('basis'), 'must render the basis (known/announced/unknown/measured/estimated), never silently drop it');
  assert.ok(/unknown_deployment_ids/.test(src), 'compare must surface deployments the install has never heard of');
  assert.ok(src.includes("t('unknown')") || /basis === 'unknown'/.test(src) || src.includes('BASIS_WORD'),
    'the unknown state must render literally, never as a blank cell');
  assert.ok(src.includes('selected.length < 2') || src.includes('selected.length >= 2'), 'compare must require at least 2 deployments before it can run');
  assert.ok(src.includes('>= 3') || src.includes('> 3'), 'the selection must cap at 3 deployments (MOD-11: side-by-side of 2-3)');
}

// ── CreatorScreen.tsx: SOLO a tab/link added, nothing else disturbed ──
{
  const src = read('studio/src/screens/creator/CreatorScreen.tsx');
  assert.ok(src.includes("import { ModelExplorer } from './ModelExplorer'"), 'CreatorScreen.tsx must import ModelExplorer');
  assert.ok(src.includes('<ModelExplorer'), 'CreatorScreen.tsx must render <ModelExplorer ... /> somewhere');
  assert.ok(src.includes('role="tablist"') || src.includes("role='tablist'"), 'the Creator/Models switch must be an accessible tab list');
  assert.ok(src.includes("t('Models')"), 'the new tab must be labelled Models');
  // the pre-existing workspace grid, document panel and preflight card must still be present unchanged in shape
  assert.ok(src.includes('fs-creator__grid'), 'the original workspace grid must still exist');
  assert.ok(src.includes('PreflightCard'), 'the original PreflightCard must still exist');
  assert.ok(src.includes('RevisionConflictError'), 'the original 409 revision-conflict handling must still exist');
}

// ── backend: model_explorer.py never invents, compare() marks unknown ──
{
  const py = read('src/creator/model_explorer.py');
  assert.ok(py.includes('BASIS_UNKNOWN'), 'must define an explicit unknown basis');
  assert.ok(py.includes('def compare('), 'must define compare(deployment_ids, task)');
  assert.ok(py.includes('fit_verdict'), 'the VRAM verdict must be imported from routes/local_models_routes.py, not reimplemented');
  assert.ok(!/def fit_verdict\(/.test(py), 'must not redefine fit_verdict locally (no parallel authority)');
  assert.ok(py.includes('unknown_deployment_ids'), 'compare() must report deployment ids it has no record of');
}
{
  const routes = read('routes/creator_model_explorer_routes.py');
  assert.ok(routes.includes('creator_enabled'), 'routes must be gated on creator_enabled');
  assert.ok(routes.includes('require_user'), 'routes must require an authenticated user');
  assert.ok(routes.includes('/models/explorer'), 'must expose GET /api/creator/models/explorer');
  assert.ok(routes.includes('/models/compare'), 'must expose POST /api/creator/models/compare');
}
{
  const app = read('app.py');
  assert.ok(app.includes('setup_creator_model_explorer_routes'), 'app.py must wire the model explorer router');
}

// ── i18n: every new string this lot introduces has a Spanish row ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  const mustHave = [
    'Models', 'Compare', 'Field', 'Task', 'Deployments', 'Capabilities', 'Footprint & VRAM',
    'Use for this task', 'Saved to project preferences.', 'No known deployments yet',
    'Parameter contracts', 'Search models',
  ];
  for (const key of mustHave) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

// ── esbuild: bundle the adapter standalone and exercise decoding against a
// fake fetch, including a compare() response with an unknown_deployment_ids
// entry, so this proves the "never invent, mark unknown" contract survives
// the bundle, not just that the source text says so ──
{
  const tmp = mkdtempSync(join(tmpdir(), 'creator-model-explorer-check-'));
  try {
    const bundlePath = join(tmp, 'creator-models-adapter.mjs');
    await esbuildBuild({
      entryPoints: [path('studio/src/adapters/creator_models.ts')],
      bundle: true,
      format: 'esm',
      platform: 'browser',
      outfile: bundlePath,
      logLevel: 'silent',
    });

    const harnessPath = join(tmp, 'harness.mjs');
    const harness = `
      import * as m from ${JSON.stringify(bundlePath)};

      const calls = [];
      globalThis.fetch = async (url, init) => {
        calls.push(String(url));
        if (String(url).includes('/models/explorer/dep-1')) {
          return new Response(JSON.stringify({
            deployment_id: 'dep-1', model_spec: { vendor: 'ollama', model_id: 'qwen3:8b' },
            deployment: {}, capability_profile: { deployment_id: 'dep-1', model_spec_id: '', axes: {} },
            param_schemas: [], footprint: { size_bytes: 0, basis: 'unknown', vram_state: '', vram_note: '', reason: 'no evidence' },
            legacy_calibration: null,
          }), { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        if (String(url).includes('/models/explorer')) {
          return new Response(JSON.stringify({ deployments: [{ deployment_id: 'dep-1', model_spec_id: '', vendor: 'ollama', family: '', model_id: 'qwen3:8b', license: '', context_tokens: 0, engine: {}, endpoint_id: '', known_axes: [], announced_axes: [] }] }), { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        if (String(url).includes('/models/compare')) {
          return new Response(JSON.stringify({
            task: 'chat.completions', deployment_ids: ['dep-1', 'ghost'],
            rows: [{ field: 'task_support', label: 'Task support', cells: {
              'dep-1': { value: 'unknown', basis: 'unknown' }, ghost: { value: null, basis: 'unknown' },
            } }],
            unknown_deployment_ids: ['ghost'],
          }), { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        return new Response('not found', { status: 404 });
      };

      const list = await m.listExplorer();
      if (!Array.isArray(list.deployments) || list.deployments[0].model_id !== 'qwen3:8b') {
        throw new Error('listExplorer did not decode: ' + JSON.stringify(list));
      }

      const entry = await m.getExplorerEntry('dep-1');
      if (entry.footprint.basis !== 'unknown') throw new Error('footprint basis not decoded verbatim: ' + JSON.stringify(entry.footprint));

      const table = await m.compareDeployments(['dep-1', 'ghost'], 'chat.completions');
      if (!table.unknown_deployment_ids.includes('ghost')) throw new Error('compare must surface unknown_deployment_ids: ' + JSON.stringify(table));
      const ghostCell = table.rows[0].cells.ghost;
      if (ghostCell.basis !== 'unknown' || ghostCell.value !== null) {
        throw new Error('an unknown deployment must never get an invented cell value: ' + JSON.stringify(ghostCell));
      }

      if (calls.length !== 3) throw new Error('expected exactly 3 fetch calls, got ' + calls.length);

      console.log('harness ok');
    `;
    writeFileSync(harnessPath, harness, 'utf8');
    const out = execFileSync(process.execPath, [harnessPath], { encoding: 'utf8' });
    assert.ok(out.includes('harness ok'), `bundled adapter harness did not pass: ${out}`);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}

console.log('ok creator-model-explorer');
