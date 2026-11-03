// The Recipes screen's reasoning (studio/src/adapters/recipes.ts).
//
//   - a dry run is structural and is NEVER drawn as verified, whatever the
//     server's `verified` field says;
//   - only a real execution that ended `verified` may claim it;
//   - an unknown status reads down (neutral tone), never as success;
//   - the parameter / criteria forms parse and refuse bad input in words.
//
// Run by tests/test_studio_recipes_js.py, or by hand: node studio/checks/recipes.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-recipes-'));
const out = join(dir, 'recipes.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'adapters', 'recipes.ts')],
  bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent',
});
const r = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (condition, message) => {
  if (!condition) { failed += 1; console.error('FAIL:', message); } else console.log('ok:', message);
};

// dry run
const dry = r.readRun({ kind: 'structural_dry_run', mode: 'dry_run', verified: true, executed: false, status: 'verified',
  steps: [{ index: 1, tool: 'write_file', status: 'resolved', reason: '', args: {} }], promotable: true }, 'dry_run');
assert(dry.structural === true, 'a dry run is structural');
assert(dry.verified === false, 'a dry run never reads as verified, even if the wire says so');
assert(r.canClaimVerified(dry) === false, 'and cannot claim verification');
assert(dry.steps[0].status === 'resolved', 'its steps only say they render');

// real runs
const base = { mode: 'execute', executed: true, steps: [{ index: 1, tool: 'write_file', status: 'ok', ids: { run_id: 'x' }, why_run: ['new case'] }],
  criteria: [{ kind: 'file_exists', path: 'a.md', passed: true }] };
const ok = r.readRun({ ...base, status: 'verified', verified: true }, 'execute');
assert(r.canClaimVerified(ok) === true, 'a real run that ended verified may claim it');
assert(ok.steps[0].ids.run_id === 'x' && ok.steps[0].whyRun[0] === 'new case', 'step ids and why_run are kept');
for (const status of ['criteria_failed', 'step_failed', 'incomplete', 'executed_unverified', 'not_runnable', 'weird']) {
  const rep = r.readRun({ ...base, status, verified: false }, 'execute');
  assert(r.canClaimVerified(rep) === false, `${status} cannot claim verification`);
}
assert(r.canClaimVerified(r.readRun({ ...base, status: 'verified', verified: false }, 'execute')) === false,
  'status verified with verified:false does not claim either');

// statuses
assert(r.statusInfo('verified').tone === 'good', 'verified is good');
assert(r.statusInfo('criteria_failed').tone === 'bad' && r.statusInfo('step_failed').tone === 'bad', 'failures are bad');
assert(r.statusInfo('incomplete').tone === 'warn' && r.statusInfo('executed_unverified').tone === 'warn', 'unfinished / unchecked are warnings');
assert(r.statusInfo('something_new').tone === 'unknown', 'an unknown status reads down to neutral');
assert(r.statusInfo('').tone === 'unknown', 'no status is neutral');
assert(r.stepStatusInfo('queued').tone === 'warn' && r.stepStatusInfo('error').tone === 'bad', 'step statuses');
assert(r.stepStatusInfo('resolved').tone === 'unknown', 'a step that merely renders is not a success tone');

// recipe and verification readers
const recipe = r.readRecipe({ id: 'rc1', title: 'Informe', status: 'draft', version: 'v1',
  params: [{ name: 'src', kind: 'path', example: 'a.csv' }, { name: 'n', kind: 'number', example: 3 }],
  step_templates: [{ index: 1, tool: 'read_file', args: { path: '{{src}}' } }],
  exit_criteria: [{ kind: 'file_exists', path: '{{src}}' }], executions: [{ at: 1, status: 'verified', verified: true, params: {}, steps: [], criteria: [] }] });
assert(recipe.learned && recipe.params.length === 2 && recipe.executions.length === 1, 'a learned recipe is read in full');
assert(!r.readRecipe({ id: 'b', title: 'Built-in', steps: ['x'] }).learned, 'a recipe with no step templates is not runnable');
const form = r.initialParams(recipe);
assert(form.src === 'a.csv' && form.n === '3', 'the form starts from the first case');
const typed = r.typedParams(recipe, { src: 'b.csv', n: '4' });
assert(typed.src === 'b.csv' && typed.n === 4, 'numbers are typed by kind, text stays text');
const v = r.readVerification({ cases: [{ case_key: 'k', status: 'verified', verified: true, fresh: false, stale: true, stale_reasons: ['an input changed'], params: {}, at: 5 }] });
assert(v[0].stale && !v[0].fresh && v[0].staleReasons[0] === 'an input changed', 'stale reasons survive');

// forms
assert(r.parseParamsJson('{"a": 1}').ok === true, 'params JSON parses');
assert(r.parseParamsJson('[1]').ok === false && r.parseParamsJson('{oops').ok === false, 'bad params JSON is refused');
assert(r.parseCriteriaJson('').ok === true && r.parseCriteriaJson('').value.length === 0, 'empty criteria are allowed');
assert(r.parseCriteriaJson('[{"kind":"contains","path":"a","text":"x"}]').ok === true, 'a valid criterion parses');
assert(r.parseCriteriaJson('[{"kind":"magic"}]').ok === false, 'an unknown criterion kind is refused');
assert(r.parseCriteriaJson('{"kind":"contains"}').ok === false, 'criteria must be a list');
assert(JSON.parse(r.criteriaExample(recipe))[0].path === '{{src}}', 'the criteria example uses a path parameter');

if (failed) { console.error(`${failed} FAILED`); process.exit(1); }
console.log('ALL OK');
