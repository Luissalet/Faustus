// CMP-09/CMP-12 (W2-F, CONTRATO_CMP_W2.md) — Studio: the strategy/recipe
// adapter and the two Composer.tsx selectors (profile fast/balanced/
// deep_review, active recipe). Static source inspection, like
// topology.check.mjs: this is JSX wiring inside one large component, not
// pure logic a bundled import can exercise on its own.
//
// Run by tests/test_cmp09_strategy_js.py, or by hand:
//   node studio/checks/strategy.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

// ── adapters/strategy.ts: the four endpoints, correctly shaped ──
{
  const p = 'studio/src/adapters/strategy.ts';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes("/api/strategy/profile"), 'must call /api/strategy/profile');
  assert.ok(src.includes("method: 'PUT'"), 'saveStrategyProfile must PUT');
  assert.ok(src.includes('/api/strategy/preview'), 'previewStrategy must call /api/strategy/preview');
  assert.ok(src.includes('/api/recipes'), 'loadRecipes must call /api/recipes');
  assert.ok(src.includes('/api/recipes/from-run/'), 'createRecipeFromRun must call /api/recipes/from-run/{run_id}');
  for (const exported of [
    'export async function loadStrategyProfile', 'export async function saveStrategyProfile',
    'export async function previewStrategy', 'export async function loadRecipes',
    'export async function createRecipeFromRun', 'export const STRATEGY_PROFILES',
  ]) {
    assert.ok(src.includes(exported), `strategy.ts must export: ${exported}`);
  }
  // wire shape: snake_case on the wire, camelCase in the typed return —
  // never leaking the raw server shape into a screen.
  for (const field of ['time_s', 'models_hint', 'permissions_needed', 'close_criteria', 'success_conditions']) {
    assert.ok(src.includes(field), `strategy.ts must map wire field: ${field}`);
  }
}

// ── screens/studio/Composer.tsx: both selectors wired, reading the
// adapter — never fetch() of their own ──
{
  const p = 'studio/src/screens/studio/Composer.tsx';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes("from '../../adapters/strategy'"), 'Composer.tsx must import from adapters/strategy');
  assert.ok(src.includes('function StrategyProfileSelector'), 'must define StrategyProfileSelector');
  assert.ok(src.includes('function RecipeSelector'), 'must define RecipeSelector');
  assert.ok(src.includes('<StrategyProfileSelector') && src.includes('<RecipeSelector'), 'both selectors must be rendered');
  assert.ok(src.includes("data-testid=\"studio-strategy-profile\""), 'strategy profile chip must carry a stable testid');
  assert.ok(src.includes("data-testid=\"studio-recipe-selector\""), 'recipe chip must carry a stable testid');
  // three profiles, exactly as the contract names them
  for (const value of ["value: 'fast'", "value: 'balanced'", "value: 'deep_review'"]) {
    assert.ok(src.includes(value), `STRATEGY_PROFILE_CHOICES must include ${value}`);
  }
  assert.ok(!/fetch\(['"]\/api\/strategy/.test(src), 'Composer.tsx must go through adapters/strategy.ts, not fetch() the endpoints directly');
  assert.ok(!/fetch\(['"]\/api\/recipes/.test(src), 'Composer.tsx must go through adapters/strategy.ts, not fetch() /api/recipes directly');
}

// ── docs/recipes/*.json: the four built-ins the contract names, each a
// well-formed Recipe (same shape src/recipes.py::Recipe.from_dict reads) ──
{
  const dir = 'docs/recipes';
  assert.ok(existsSync(path(dir)), `missing ${dir}`);
  const expectedIds = ['review-changes', 'sources-to-report', 'design-function-and-tests', 'edit-passage-keep-tone'];
  const { readdirSync } = await import('node:fs');
  const files = readdirSync(path(dir)).filter((f) => f.endsWith('.json'));
  const recipes = files.map((f) => JSON.parse(read(join(dir, f))));
  const ids = recipes.map((r) => r.id).sort();
  assert.deepEqual(ids, [...expectedIds].sort(), `docs/recipes/*.json ids must be exactly ${expectedIds.join(', ')}`);
  for (const recipe of recipes) {
    for (const field of ['id', 'title', 'inputs', 'steps', 'tools', 'success_conditions', 'optional_resources']) {
      assert.ok(field in recipe, `${recipe.id ?? '?'} is missing field: ${field}`);
    }
    assert.ok(Array.isArray(recipe.steps) && recipe.steps.length > 0, `${recipe.id} must have at least one step`);
    assert.equal(recipe.status, 'published', `${recipe.id} must ship as status: published`);
  }
}

// ── i18n: every new string this lot introduces has a Spanish row (see
// topology.check.mjs's own comment for why this narrower, lot-scoped
// tripwire exists alongside the repo-wide `--check`) ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  const mustHave = [
    'Fast', 'Balanced', 'Deep review', 'Strategy profile', 'Recipe', 'No recipe',
    'Could not save the strategy profile.', 'Could not save the active recipe.',
  ];
  for (const key of mustHave) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

console.log('ok strategy');
