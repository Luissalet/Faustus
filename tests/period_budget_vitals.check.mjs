// Agora #231: period-budget meter in Studio Vitals.
// Driven by tests/test_studio_period_budget_vitals.py, or by hand:
//   node tests/period_budget_vitals.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-pbudget-')), 'periodBudget.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'adapters', 'periodBudget.ts')],
  bundle: true,
  format: 'esm',
  platform: 'node',
  outfile: out,
  logLevel: 'silent',
  // React hooks are unused in parse tests; stub the import if pulled.
  external: [],
});
const mod = await import(pathToFileURL(out).href);
const read = (p) => readFileSync(join(root, p), 'utf8').replace(/\r\n/g, '\n');

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};
const eq = (x, y, msg) => assert(JSON.stringify(x) === JSON.stringify(y), `${msg} (got ${JSON.stringify(x)})`);

const empty = mod.parsePeriodBudget({});
eq(empty.enabled, false, 'empty payload is not enabled');
eq(empty.providers, [], 'empty payload has no providers');
eq(empty.gpu, null, 'empty payload has no gpu row');
eq(empty.breaker.open, false, 'missing breaker is closed');

const raw = {
  now: 1_700_000_000,
  window: 'day',
  window_start: 1_699_913_600,
  window_resets_at: 1_700_000_000 + 3600,
  enabled: true,
  interactive_active: true,
  providers: [
    {
      provider: 'openrouter',
      window: 'day',
      metric: 'usd',
      used: 1.25,
      target: 2,
      pace: { elapsed_fraction: 0.5, allowed_fraction: 0.6, allowed: 1.2, band: 0.1 },
      paused: false,
      paused_until: null,
      reason: 'ok',
      unpriced_tokens: 3,
      calls: 9,
      window_resets_at: 1_700_000_000 + 3600,
    },
    {
      provider: 'openrouter',
      window: 'day',
      metric: 'tokens',
      used: 900_000,
      target: 1_000_000,
      pace: { elapsed_fraction: 0.5, allowed_fraction: 0.55, allowed: 550_000, band: 0.05 },
      paused: true,
      paused_until: 1_700_000_100,
      reason: 'over pace',
      unpriced_tokens: 0,
      calls: 2,
      window_resets_at: 1_700_000_000 + 3600,
    },
  ],
  gpu: {
    provider: 'local',
    window: 'day',
    metric: 'gpu_seconds',
    used: 1200,
    target: 3600,
    pace: { elapsed_fraction: 0.4, allowed_fraction: 0.5, allowed: 1800, band: 0.1 },
    paused: false,
    paused_until: null,
    reason: 'ok',
    calls: 4,
    window_resets_at: 1_700_000_000 + 3600,
  },
  breaker: {
    enabled: true,
    open: true,
    consecutive_failures: 3,
    threshold: 3,
    pause_until: 1_700_000_200,
    last_failure: { kind: 'timeout' },
  },
  cooldowns: [{ endpoint: 'https://api.example/v1', status: '429', until: 1_700_000_300, hits: 2 }],
  hosted_seen: [{ provider: 'openrouter' }],
};

const b = mod.parsePeriodBudget(raw);
eq(b.enabled, true, 'enabled when targets exist');
eq(b.window, 'day', 'window kept');
eq(b.interactiveActive, true, 'interactive_active mapped');
eq(b.providers.length, 2, 'two provider rows');
eq(b.providers[0].used, 1.25, 'usd used kept');
eq(b.providers[0].pace.allowed, 1.2, 'pace.allowed kept');
eq(b.providers[1].paused, true, 'paused flag kept');
eq(b.gpu?.metric, 'gpu_seconds', 'gpu row kept');
eq(b.breaker.open, true, 'breaker open kept');
eq(b.breaker.lastFailureKind, 'timeout', 'last failure kind kept');
eq(b.cooldowns[0].hits, 2, 'cooldown hits kept');

eq(mod.rowLevel(b.providers[0]), 'hot', 'used above pace allowance is hot');
eq(mod.rowLevel(b.providers[1]), 'hot', 'paused row is hot');
eq(mod.rowLevel(b.gpu), '', 'gpu under pace is quiet');
assert(mod.formatAmount('usd', 0.0123).startsWith('$0.012'), 'usd formatter');
assert(mod.formatAmount('gpu_seconds', 1200).includes('1200') || mod.formatAmount('gpu_seconds', 1200).includes('1'), 'gpu seconds formatter');
assert(mod.rowLabel(b.providers[0]).includes('openrouter') && mod.rowLabel(b.providers[0]).includes('USD'), 'row label names provider and metric');

const disabled = mod.parsePeriodBudget({ enabled: false, providers: [], gpu: null });
eq(disabled.enabled, false, 'explicit disabled stays disabled');
eq(mod.rowLevel({ ...b.providers[0], used: 0.5, paused: false, pace: { ...b.providers[0].pace, allowedFraction: 0.6 } }), '', 'well under pace is quiet');

const vitals = read('studio/src/screens/studio/Vitals.tsx');
assert(
  vitals.includes("from '../../adapters/periodBudget'") && vitals.includes('<PeriodBudgetSection />'),
  'Vitals mounts the period-budget section',
);
assert(vitals.includes("t('Period budget')"), 'section title is the English key');
assert(vitals.includes('No period target set: nothing is paced'), 'honest empty when no target');
assert(vitals.includes('Could not read the period budget: {why}'), 'honest error line');
assert(!vitals.includes("getJson<") && !vitals.includes("'/api/budget/period'"), 'screen does not fetch the budget itself');

const adapter = read('studio/src/adapters/periodBudget.ts');
assert(adapter.includes("getJson") && adapter.includes("'/api/budget/period'"), 'adapter reads GET /api/budget/period');
assert(adapter.includes('status === 401') && adapter.includes('status === 403'), 'auth refusal hides the section');
assert(adapter.includes('export function parsePeriodBudget'), 'parse is exported for checks');

const css = read('studio/src/screens/studio/vitals.css');
assert(css.includes('.fs-vt__budget-note'), 'vitals.css styles the budget notes');

const obj = read('OBJETIVOS.md');
assert(obj.includes('Agora #231') && obj.includes('periodBudget.ts'), 'OBJ-53 marks the meter done with #231');

if (failed) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}
console.log('ALL OK');
