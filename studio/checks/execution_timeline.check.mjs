// INF-03 (Lote B — Studio) — "why did it take this long?" timeline helpers.
// Drives the real `studio/src/adapters/chat.ts` (`executionMetricsFrom`,
// `timelineBars`) through esbuild rather than re-implementing their logic
// in Python — same pattern `studio/checks/serve_veracity.check.mjs` uses.
//
// Covers the one rule everything in CONTRATO_INF03.md answers to: a phase
// that was not observed (`absent`) never produces a bar — not even a
// zero-length one — and phases that overlap (sum past `total_ms`) are
// flagged, never silently rescaled to fit.
//
// Run by tests/test_inf03_timeline_js.py, or by hand:
//   node studio/checks/execution_timeline.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-inf03-timeline-')), 'chat.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'adapters', 'chat.ts')],
  bundle: true,
  format: 'esm',
  platform: 'node',
  outfile: out,
  logLevel: 'silent',
});
const chat = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

const mv = (value, source) => ({ value, source });
const ABSENT = mv(null, 'absent');

function execution(phases, tokens = {}, notes = []) {
  return chat.executionMetricsFrom({
    schema_version: 1,
    phases,
    tokens,
    scope: 'request',
    engine: null,
    observed_at: '2026-01-01T00:00:00.000Z',
    notes,
  });
}

// ── T01: docs/api/execution_metrics.md's own worked example ────────────────
{
  const exec = execution({
    queue_wait_ms: { value: 12.5, source: 'observed_client' },
    load_ms: { value: 50.0, source: 'reported_engine' },
    prefill_ms: { value: 200.0, source: 'reported_engine' },
    generation_ms: { value: 900.0, source: 'reported_engine' },
    tools_ms: { value: null, source: 'absent' },
    total_ms: { value: 1180.5, source: 'observed_client' },
  }, { prompt: { value: 128, source: 'reported_engine' }, generated: { value: 64, source: 'reported_engine' } });
  const tl = chat.timelineBars(exec);

  assert(tl.bars.length === 5, `T01: 5 bars (all phases but tools_ms) — got ${tl.bars.length}`);
  assert(tl.absentPhases.length === 1 && tl.absentPhases[0] === 'tools_ms', 'T01: tools_ms is the one absent phase');
  const total = tl.bars.find((b) => b.phase === 'total_ms');
  assert(total.widthPercent === 100, 'T01: total_ms is always 100% of itself');
  const gen = tl.bars.find((b) => b.phase === 'generation_ms');
  assert(Math.abs(gen.widthPercent - (900 / 1180.5) * 100) < 1e-6, 'T01: generation_ms width is value/total');
  assert(tl.overlap === false, 'T01: a clean example does not overlap (12.5+50+200+900 < 1180.5... well within total)');
  assert(tl.tokens.prompt.value === 128 && tl.tokens.prompt.source === 'reported_engine', 'T01: prompt tokens pass through');
}

// ── T02: `absent` never produces a bar, not even a zero-length one ─────────
{
  const exec = execution({
    queue_wait_ms: ABSENT,
    load_ms: ABSENT,
    prefill_ms: ABSENT,
    generation_ms: ABSENT,
    tools_ms: ABSENT,
    total_ms: { value: 500, source: 'observed_client' },
  });
  const tl = chat.timelineBars(exec);
  assert(tl.bars.length === 1 && tl.bars[0].phase === 'total_ms', 'T02: only total_ms bars when everything else is absent (T07 shape)');
  assert(tl.absentPhases.length === 5, 'T02: the other five phases are all listed absent');
  assert(!tl.absentPhases.includes('total_ms'), 'T02: total_ms itself is not in absentPhases');
}

// A malformed/absent MetricValue coming off the wire (value present but
// source absent, or vice versa) degrades to a true absent — never a bar
// drawn from half-parsed data.
{
  const exec = execution({
    queue_wait_ms: { value: 12, source: 'absent' }, // source says absent: value must not leak through
    load_ms: { value: null, source: 'reported_engine' }, // no value: can't be a bar either
    prefill_ms: { value: 10, source: 'not_a_real_source' }, // unrecognised source
    generation_ms: ABSENT,
    tools_ms: ABSENT,
    total_ms: { value: 500, source: 'observed_client' },
  });
  const tl = chat.timelineBars(exec);
  assert(tl.absentPhases.includes('queue_wait_ms'), 'T02b: value alongside source:absent still yields absent, no bar');
  assert(tl.absentPhases.includes('load_ms'), 'T02b: source alongside value:null still yields absent, no bar');
  assert(tl.absentPhases.includes('prefill_ms'), 'T02b: an unrecognised source degrades to absent, no bar');
}

// ── T03: phases that overlap the total are flagged, never rescaled ─────────
{
  const exec = execution({
    queue_wait_ms: ABSENT,
    load_ms: ABSENT,
    prefill_ms: { value: 200, source: 'reported_engine' },
    generation_ms: { value: 900, source: 'reported_engine' },
    tools_ms: { value: 400, source: 'observed_client' },
    total_ms: { value: 1000, source: 'observed_client' }, // 200+900+400 = 1500 > 1000
  }, {}, ['phases overlap: engine and client clocks are not additive']);
  const tl = chat.timelineBars(exec);
  assert(tl.overlap === true, 'T03: prefill+generation+tools exceeding total sets overlap');
  const gen = tl.bars.find((b) => b.phase === 'generation_ms');
  assert(Math.abs(gen.widthPercent - 90) < 1e-6, 'T03: generation_ms keeps its true (90%) width, not rescaled to fit under 100');
  const tools = tl.bars.find((b) => b.phase === 'tools_ms');
  assert(Math.abs(tools.widthPercent - 40) < 1e-6, 'T03: tools_ms keeps its true (40%) width too — the three do not get shrunk to sum to 100');
  assert(tl.notes.includes('phases overlap: engine and client clocks are not additive'), 'T03: the contract note passes through verbatim');
}

// A phase that individually exceeds total_ms (clock disagreement at the
// extreme) is not clamped by timelineBars either — over 100% is a true
// fact about disagreeing clocks, not something to hide by capping the number.
{
  const exec = execution({
    queue_wait_ms: ABSENT,
    load_ms: ABSENT,
    prefill_ms: ABSENT,
    generation_ms: { value: 1500, source: 'inferred' },
    tools_ms: ABSENT,
    total_ms: { value: 1000, source: 'observed_client' },
  });
  const tl = chat.timelineBars(exec);
  const gen = tl.bars.find((b) => b.phase === 'generation_ms');
  assert(gen.widthPercent === 150, 'T03b: a single phase over total keeps its true >100% width from timelineBars (a renderer, not this helper, caps the drawn box)');
  assert(gen.source === 'inferred', 'T03b: source passes through unchanged');
}

// ── T04: no overlap when phases comfortably fit under total ────────────────
{
  const exec = execution({
    queue_wait_ms: { value: 5, source: 'observed_client' },
    load_ms: ABSENT,
    prefill_ms: { value: 50, source: 'reported_engine' },
    generation_ms: { value: 100, source: 'reported_engine' },
    tools_ms: ABSENT,
    total_ms: { value: 200, source: 'observed_client' },
  });
  const tl = chat.timelineBars(exec);
  assert(tl.overlap === false, 'T04: 5+50+100 well under 200 — no overlap');
}

// ── T05: a turn interrupted before its first token (T07 in the backend
//     suite) — total_ms is the one phase that always has a value ──────────
{
  const exec = execution({
    queue_wait_ms: ABSENT,
    load_ms: ABSENT,
    prefill_ms: ABSENT,
    generation_ms: ABSENT,
    tools_ms: ABSENT,
    total_ms: { value: 42, source: 'observed_client' },
  });
  const tl = chat.timelineBars(exec);
  assert(tl.bars.length === 1 && tl.bars[0].valueMs === 42, 'T05: total_ms alone still bars, all other phases absent');
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nINF-03 timeline: all checks passed');
