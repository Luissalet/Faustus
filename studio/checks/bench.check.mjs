// INF-04 (Lote B — Studio) — the local-inference benchmark screen's pure
// presentation helpers. Drives the real `studio/src/adapters/bench.ts`
// through esbuild rather than re-implementing its logic in Python — same
// pattern `studio/checks/execution_timeline.check.mjs`/`side_threads.check.mjs`
// use.
//
// Covers the rules CONTRATO_INF04.md's Lote B answers to: `estimate_seconds
// === null` reads as "unknown until a first run" (never a guessed number,
// never a bare dash); `canPromote` is true ONLY for a comparable
// `verdict === "improvement"` (§13's promotion gate, restated as the one
// boolean "Mark as recommended" checks); `isRunInFlight` names exactly the
// states `Optimize.tsx` polls on.
//
// Whether `Optimize.tsx` itself ever calls `startBench`/`start` from inside
// a `useEffect` (the "opening the screen never starts anything" rule) is
// checked separately, by a static source scan in
// `tests/test_inf04_bench_js.py` — the same technique
// `tests/test_studio_guards.py` already uses for Studio-wide guards, and
// cheaper here than teaching esbuild to inspect JSX effect bodies.
//
// Run by tests/test_inf04_bench_js.py, or by hand:
//   node studio/checks/bench.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-inf04-bench-')), 'bench.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'adapters', 'bench.ts')],
  bundle: true,
  format: 'esm',
  platform: 'node',
  outfile: out,
  logLevel: 'silent',
});
const bench = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

// ── estimateLabel: null never becomes a guessed number ──────────────────────
assert(bench.estimateLabel(null) === 'unknown until a first run', 'estimateLabel(null) is the contract wording, not a dash');
assert(bench.estimateLabel(0.4) === 'under a second', 'estimateLabel: sub-second stays "under a second"');
assert(bench.estimateLabel(45) === '~45s', 'estimateLabel: whole seconds under a minute');
assert(bench.estimateLabel(125) === '~2.1 min', 'estimateLabel: minutes, one decimal (125s = 2.0833min -> 2.1)');
assert(bench.estimateLabel(7200) === '~2 h', 'estimateLabel: whole hours');
assert(bench.estimateLabel(5400) === '~1.5 h', 'estimateLabel: fractional hours keep one decimal');

// ── formatDelta: a percentage change, never a bare number ──────────────────
assert(bench.formatDelta(null) === 'n/a', 'formatDelta(null) is n/a, not 0% or a dash');
assert(bench.formatDelta(12.34) === '+12.3%', 'formatDelta: positive gets an explicit +');
assert(bench.formatDelta(-4.5) === '-4.5%', 'formatDelta: negative keeps its own sign');
assert(bench.formatDelta(0) === '0.0%', 'formatDelta: exactly zero gets no sign');

// ── verdictTone: only "improvement" reads as ok, only "regression" as danger ─
assert(bench.verdictTone('improvement') === 'ok', 'verdictTone: improvement is ok');
assert(bench.verdictTone('regression') === 'danger', 'verdictTone: regression is danger');
assert(bench.verdictTone('no_change') === 'warning', 'verdictTone: no_change is warning, not ok');
assert(bench.verdictTone('inconclusive') === 'warning', 'verdictTone: inconclusive is warning, not ok');

// ── canPromote: §13's gate, restated ────────────────────────────────────────
assert(bench.canPromote({ verdict: 'improvement', comparable: true }) === true, 'canPromote: comparable improvement -> true');
assert(bench.canPromote({ verdict: 'improvement', comparable: false }) === false, 'canPromote: an incomparable improvement never promotes (comparable=false rule)');
assert(bench.canPromote({ verdict: 'regression', comparable: true }) === false, 'canPromote: regression never promotes, however fast');
assert(bench.canPromote({ verdict: 'no_change', comparable: true }) === false, 'canPromote: no_change never promotes');
assert(bench.canPromote({ verdict: 'inconclusive', comparable: true }) === false, 'canPromote: inconclusive never promotes');
assert(bench.canPromote(null) === false, 'canPromote: no comparison at all -> false');

// ── isRunInFlight: exactly the states Optimize.tsx polls on ────────────────
for (const s of ['waiting_resources', 'preparing', 'running', 'evaluating']) {
  assert(bench.isRunInFlight(s) === true, `isRunInFlight('${s}') is true`);
}
for (const s of ['planned', 'completed', 'partial', 'cancelled', 'interrupted', 'failed']) {
  assert(bench.isRunInFlight(s) === false, `isRunInFlight('${s}') is false`);
}
assert(
  bench.RUN_STATES_IN_FLIGHT.length === 4
    && ['waiting_resources', 'preparing', 'running', 'evaluating'].every((s) => bench.RUN_STATES_IN_FLIGHT.includes(s)),
  'RUN_STATES_IN_FLIGHT names exactly the four in-flight states',
);

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nINF-04 bench: all checks passed');
