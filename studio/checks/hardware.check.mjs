// INF-05 (Lote C — Studio) — physical GPU topology, the desegregated
// per-GPU memory budget, and the three context limits. Drives the real
// `studio/src/adapters/hardware.ts` through esbuild rather than
// re-implementing its logic in Python — same pattern
// `studio/checks/bench.check.mjs` uses for INF-04.
//
// Covers the rules CONTRATO_INF05.md's Lote C answers to: `absent`/`null`
// on the wire never becomes a bare `0` (§11), a "narrow observed link"
// heuristic note only ever fires when the TRANSPORT itself is `heuristic`
// (never from a GPU's commercial name), an index is never identity (§11
// T15's `reconciliationLabel`), a `CandidateEstimate` that is not
// `complete` reads as a floor/"incomplete", never false precision (§11),
// and the three context limits (`nativeContextLabel`/
// `configuredContextLabel`/`evaluatedContextLabel`) stay SEPARATE (§14).
//
// Whether `Servers.tsx` ever calls `annotateTopology(`/`refreshHardware`'s
// underlying reads from inside a `useEffect` in a way that fires without a
// person's click, and whether `Optimize.tsx` ever calls
// `activateProfile(`/`deactivateProfile(` from inside a `useEffect`, is
// checked separately by a static source scan in
// `tests/test_inf05_hardware_js.py` — the same technique
// `tests/test_inf04_bench_js.py` already uses for `startBench`.
//
// Run by tests/test_inf05_hardware_js.py, or by hand:
//   node studio/checks/hardware.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-inf05-hardware-')), 'hardware.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'adapters', 'hardware.ts')],
  bundle: true,
  format: 'esm',
  platform: 'node',
  outfile: out,
  logLevel: 'silent',
});
const hw = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

const absentComponent = { bytes: null, source: 'absent', note: '' };

// ── parsers: absent/null stays null, NEVER a bare 0 (§11) ──────────────────
assert(hw.parseMemoryComponent({}).bytes === null, 'parseMemoryComponent: missing bytes stays null, not 0');
assert(hw.parseMemoryComponent({ bytes: 0 }).bytes === 0, 'parseMemoryComponent: an actual observed 0 stays 0 (distinct from absent)');
assert(hw.parseMemoryComponent({ bytes: 0 }).bytes !== null, 'parseMemoryComponent: 0 and null are never conflated');
assert(hw.parseMemoryComponent(null).source === 'absent', 'parseMemoryComponent: null input defaults source to absent');

const snap = hw.parseHardwareSnapshot({ host: 'box', gpus: [{ index: 0, name: 'RTX 4070 Ti', uuid: 'GPU-abc', bus_id: '0000:01:00.0' }] });
assert(snap.gpus.length === 1, 'parseHardwareSnapshot: parses the gpus list');
assert(snap.gpus[0].link === null, 'parseHardwareSnapshot: a GPU with no link key parses to null, not a zeroed LinkInfo');
assert(snap.gpus[0].transport === null, 'parseHardwareSnapshot: a GPU with no transport key parses to null');
assert(hw.parseHardwareSnapshot({}).topology === 'unknown', 'parseHardwareSnapshot: missing topology defaults to "unknown", never "known"');

const estimateAbsent = hw.parseCandidateEstimate({ weights: {}, kv_state: {}, buffers: {}, margin: {}, total_lower: null, total_upper: null, complete: false, basis: 'incomplete' });
assert(estimateAbsent.weights.bytes === null, 'parseCandidateEstimate: absent weights stays null');
assert(estimateAbsent.complete === false, 'parseCandidateEstimate: complete=false is preserved, never defaulted to true');
assert(hw.parseCandidateEstimate(null) === null, 'parseCandidateEstimate: a null estimate (nothing requested) stays null');

const budget = hw.parseMemoryBudget({
  gpu_key: 'uuid:abc', gpu_index: 0, gpu_name: 'RTX 4070 Ti', total_bytes: 12884901888,
  components: { weights_resident: { bytes: 8589934592, source: 'observed' } },
  consumers: [{ kind: 'ollama', label: 'qwen3.8', bytes: 8589934592, source: 'observed' }],
  stale: true,
});
assert(budget.stale === true, 'parseMemoryBudget: stale is preserved');
assert(budget.components.kv_state.bytes === null, 'parseMemoryBudget: an unmentioned component defaults to absent (null), not 0');
assert(budget.consumers.length === 1 && budget.consumers[0].kind === 'ollama', 'parseMemoryBudget: consumers parse through');

// ── gbLabel / componentLabel: never a bare 0 for absent ─────────────────────
assert(hw.gbLabel(null) === 'not reported', 'gbLabel(null) is spelled out, never a bare dash or 0');
assert(hw.gbLabel(0) === '0.0 GB', 'gbLabel(0): an ACTUAL observed zero is shown as a number, distinct from absent');
assert(hw.gbLabel(17179869184) === '16.0 GB', 'gbLabel: one decimal, GiB-based (the same convention bytesLabel/fmtGb use)');
assert(hw.componentLabel(absentComponent) === 'not reported', 'componentLabel: absent component never renders "0 GB"');
assert(hw.componentLabel({ bytes: 17179869184, source: 'observed', note: '' }).includes('16.0 GB'), 'componentLabel: an observed component shows its bytes');
assert(hw.componentLabel({ bytes: 17179869184, source: 'observed', note: '' }).includes('observed'), 'componentLabel: shows its source as text, not colour alone (accessibility)');

// ── source labels: text, never colour-only; every enum member maps ─────────
for (const s of ['observed', 'manual', 'heuristic', 'estimated', 'reported_engine', 'absent']) {
  assert(typeof hw.sourceLabel(s) === 'string' && hw.sourceLabel(s).length > 0, `sourceLabel('${s}') is a non-empty string`);
}
assert(hw.sourceTone('observed') === 'ok', "sourceTone('observed') reads as ok");
assert(hw.sourceTone('manual') === 'neutral', "sourceTone('manual') reads as neutral, not ok (a person's guess, not a reading)");
assert(hw.sourceTone('heuristic') === 'warning', "sourceTone('heuristic') reads as warning");
assert(hw.sourceTone('absent') === 'warning', "sourceTone('absent') reads as warning, never ok");

// ── linkLabel: the heuristic note fires ONLY from transport.source, never
// from a GPU's commercial name (§11) ────────────────────────────────────────
const wideObserved = { link: { gen_current: 4, width_current: 16, gen_max: 4, width_max: 16, source: 'observed' }, transport: null };
assert(hw.linkLabel(wideObserved).includes('Gen4') && hw.linkLabel(wideObserved).includes('x16'), 'linkLabel: a wide observed link shows gen and width');
assert(!hw.linkLabel(wideObserved).includes('narrow'), 'linkLabel: a wide link never carries a narrow note');
const narrowHeuristic = { link: { gen_current: 3, width_current: 4, gen_max: 4, width_max: 16, source: 'observed' }, transport: { kind: 'unknown', source: 'heuristic', note: 'narrow link (x4)', observed_at: null } };
assert(hw.linkLabel(narrowHeuristic).includes('narrow'), 'linkLabel: a narrow link with a heuristic transport carries the narrow note');
const narrowManual = { link: { gen_current: 3, width_current: 4, gen_max: 4, width_max: 16, source: 'observed' }, transport: { kind: 'thunderbolt', source: 'manual', note: '', observed_at: null } };
assert(!hw.linkLabel(narrowManual).includes('narrow'), 'linkLabel: a narrow link with a MANUAL (not heuristic) transport carries no narrow note — only heuristic triggers it');
assert(hw.linkLabel({ link: null, transport: null }) === 'not reported', 'linkLabel: an absent link reads as "not reported", not a bare 0 or blank');
assert(hw.linkLabel({ link: { gen_current: null, width_current: null, gen_max: null, width_max: null, source: 'absent' }, transport: null }) === 'not reported', 'linkLabel: an absent-source link reads as "not reported"');

// ── gpuIdentityKey: mirrors src.contracts.inference.identity_key exactly —
// uuid first, bus_id second, an index is NEVER identity (§11 T15) ─────────
assert(hw.gpuIdentityKey({ uuid: 'GPU-abc', bus_id: '0000:01:00.0' }) === 'uuid:GPU-abc', 'gpuIdentityKey: prefers uuid, prefixed uuid:');
assert(hw.gpuIdentityKey({ uuid: null, bus_id: '0000:01:00.0' }) === 'bus:0000:01:00.0', 'gpuIdentityKey: falls back to bus_id, prefixed bus:');
assert(hw.gpuIdentityKey({ uuid: null, bus_id: null }) === null, 'gpuIdentityKey: no uuid/bus_id -> null, never a fabricated key');

// ── reconciliationLabel / reconciliationTone: an index is never identity ───
assert(hw.reconciliationLabel({ key: 'uuid:abcdef12-3456', previous_index: 1, current_index: 2, state: 'moved' }).includes('moved'), 'reconciliationLabel: moved names itself');
assert(hw.reconciliationLabel({ key: 'uuid:abcdef12-3456', previous_index: 1, current_index: null, state: 'missing' }).includes('missing'), 'reconciliationLabel: missing names itself');
assert(hw.reconciliationLabel({ key: null, previous_index: 0, current_index: null, state: 'new' }).includes('new'), 'reconciliationLabel: new names itself');
assert(hw.reconciliationLabel({ key: null, previous_index: 0, current_index: null, state: 'unidentifiable' }).toLowerCase().includes('cannot'), 'reconciliationLabel: unidentifiable never claims "same"');
assert(hw.reconciliationTone('same') === 'ok', 'reconciliationTone: same is ok');
assert(hw.reconciliationTone('missing') === 'danger', 'reconciliationTone: missing is danger');
assert(hw.reconciliationTone('moved') === 'warning', 'reconciliationTone: moved is warning');
assert(hw.reconciliationTone('unidentifiable') === 'warning', 'reconciliationTone: unidentifiable is warning, never "same"/ok');

// ── estimateRangeLabel: complete -> a range; incomplete -> a floor with a
// reason, NEVER false precision (§11) ───────────────────────────────────────
const completeEstimate = { total_lower: 15254296576, total_upper: 16226237235, complete: true, basis: 'fitted', notes: [] };
assert(hw.estimateRangeLabel(completeEstimate).includes('–') && hw.estimateRangeLabel(completeEstimate).includes('GB'), 'estimateRangeLabel: a complete estimate reads as a range');
assert(!hw.estimateRangeLabel(completeEstimate).includes('incomplete'), 'estimateRangeLabel: a complete estimate is never marked incomplete');
const floorEstimate = { total_lower: 15254296576, total_upper: null, complete: false, basis: 'incomplete', notes: ['architecture unknown'] };
const floorLabel = hw.estimateRangeLabel(floorEstimate);
assert(floorLabel.startsWith('≥'), 'estimateRangeLabel: an incomplete estimate with a floor reads as "≥ N GB"');
assert(floorLabel.includes('incomplete'), 'estimateRangeLabel: an incomplete floor says so, never dressed up as exact');
const nothingEstimate = { total_lower: null, total_upper: null, complete: false, basis: 'incomplete', notes: [] };
assert(hw.estimateRangeLabel(nothingEstimate).includes('unknown'), 'estimateRangeLabel: nothing at all reads as unknown, never a fabricated number');
assert(hw.estimateRangeLabel(null) === 'unknown', 'estimateRangeLabel(null): no estimate requested reads as unknown');

// ── basisLabel: single_observation/fitted/metadata/incomplete, each in words
assert(hw.basisLabel({ basis: 'single_observation', validity: { ctx_min: 8192, ctx_max: 8192, slots: 1 } }).includes('8192'), 'basisLabel: single_observation names its ctx');
assert(hw.basisLabel({ basis: 'fitted', validity: { ctx_min: 4096, ctx_max: 32768, slots: 1 } }).includes('4k'), 'basisLabel: fitted names its domain, abbreviated');
assert(hw.basisLabel({ basis: 'metadata', validity: null }) === 'from metadata', 'basisLabel: metadata reads as "from metadata"');
assert(hw.basisLabel({ basis: 'incomplete', validity: null }) === 'incomplete', 'basisLabel: incomplete reads as "incomplete"');

// ── verdictLabel / verdictTone: a stale/unknown reading is never "fits" ────
assert(hw.verdictLabel({ verdict: 'fits', reason: '', shortfall_bytes: null }) === 'fits', 'verdictLabel: fits reads as fits');
assert(hw.verdictLabel({ verdict: 'does_not_fit', reason: '', shortfall_bytes: 2147483648 }).includes('short by'), 'verdictLabel: does_not_fit names the shortfall');
assert(hw.verdictLabel({ verdict: 'unknown', reason: 'reading is 42 s old; refresh before loading', shortfall_bytes: null }).includes('42 s'), 'verdictLabel: unknown carries the reason verbatim');
assert(hw.verdictTone('fits') === 'ok', 'verdictTone: fits is ok');
assert(hw.verdictTone('does_not_fit') === 'danger', 'verdictTone: does_not_fit is danger');
assert(hw.verdictTone('unknown') === 'warning', 'verdictTone: unknown is warning, never ok');

// ── the three context limits stay SEPARATE, never collapsed (§14) ──────────
const limits = hw.parseContextLimits({
  native: { value: 8192, source: 'hf_config', note: '' },
  configured: { value: 4096, source: 'receipt', note: '' },
  evaluated: { min: 2048, max: 2048, source: 'bench_runs', note: 'largest observed prompt across 3 run(s)' },
});
const line = hw.contextLimitsLine(limits);
assert(line.includes('8192') && line.includes('4096') && line.includes('2048'), 'contextLimitsLine: all three numbers appear, never collapsed into one');
assert(line.includes('native') && line.includes('configured') && line.includes('evaluated'), 'contextLimitsLine: all three limits are labelled');
const noneLimits = hw.parseContextLimits({});
assert(hw.nativeContextLabel(noneLimits.native).includes('not reported'), 'nativeContextLabel: absent native reads as not reported, never 0');
assert(hw.configuredContextLabel(noneLimits.configured).includes('not reported'), 'configuredContextLabel: absent configured reads as not reported');
assert(hw.evaluatedContextLabel(noneLimits.evaluated).includes('not reported'), 'evaluatedContextLabel: absent evaluated reads as not reported');

// ── consumerLabel / sharedSpillLabel / staleLabel ───────────────────────────
assert(hw.consumerLabel({ kind: 'ollama', label: 'qwen3.8', pid: null, bytes: 17179869184, source: 'observed' }).includes('qwen3.8'), 'consumerLabel: names the model');
assert(hw.consumerLabel({ kind: 'other_process', label: '', pid: 1234, bytes: null, source: 'estimated' }).includes('1234'), 'consumerLabel: falls back to pid when there is no label');
assert(hw.sharedSpillLabel({ bytes: 0, source: 'absent', note: '' }) === null, 'sharedSpillLabel: nothing to show when there is no spill');
assert(hw.sharedSpillLabel({ bytes: 3221225472, source: 'observed', note: '' }).includes('not VRAM'), 'sharedSpillLabel: an actual spill says "not VRAM"');
assert(hw.staleLabel({ stale: false, observed_at: null }) === null, 'staleLabel: a fresh budget shows nothing');
assert(hw.staleLabel({ stale: true, observed_at: new Date(Date.now() - 42000).toISOString() }, Date.now()).includes('42'), 'staleLabel: a stale budget names its age');

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nINF-05 hardware: all checks passed');
