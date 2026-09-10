// Lote 28: `Turn.errorClass`/`traceId`/`stepId`/`versionMismatch` — typed,
// optional, read defensively off whatever the `error`/`terminal` event
// carries. `adapters/chat.ts`'s `ChatEvent` union does not declare these
// fields yet (a separate lote owns that file), so `apply()` reads them via
// `errorTraceFields()`'s passthrough rather than off the declared type —
// this proves that passthrough actually works, and that its ABSENCE is
// silently fine (the whole point of "with fallback if not").
//
// Run by tests/test_studio_error_trace_fields_js.py, or by hand:
//   node studio/checks/error-trace-fields.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-errtrace-')), 'model.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'screens', 'studio', 'model.ts')],
  bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent',
});
const m = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => {
  if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg);
};

// ── A build that has not started sending these fields yet: today's shape ──
{
  const t0 = m.blankTurn('assistant');
  const t1 = m.apply(t0, { type: 'error', message: 'boom' });
  assert(t1.error === 'boom', 'the message still lands with no error_class at all');
  assert(t1.errorClass === undefined, 'errorClass stays undefined, not null/empty-string, when absent');
  assert(t1.traceId === undefined && t1.stepId === undefined, 'trace/step stay undefined when absent');
}

// ── Once a backend/adapter DOES send them (passthrough on the raw event) ──
{
  const t0 = m.blankTurn('assistant');
  const withExtra = { type: 'error', message: 'llm down', error_class: 'transport.llm_service_error', trace_id: 'tr_1', step_id: 'st_2' };
  const t1 = m.apply(t0, withExtra);
  assert(t1.errorClass === 'transport.llm_service_error', 'error_class is picked up once present');
  assert(t1.traceId === 'tr_1' && t1.stepId === 'st_2', 'trace_id/step_id are picked up once present');
}

// ── `terminal` (the other failure path) behaves the same way ──
{
  const t0 = m.blankTurn('assistant');
  const failedTerminal = { type: 'terminal', failed: true, message: 'model crashed', error_class: 'unknown.panic' };
  const t1 = m.apply(t0, failedTerminal);
  assert(t1.error === 'model crashed' && t1.errorClass === 'unknown.panic', 'terminal failures carry errorClass too');
  const okTerminal = m.apply(t0, { type: 'terminal', failed: false });
  assert(okTerminal.error === undefined, 'a successful terminal event does not fabricate an error');
}

// ── A later event without the field does not erase what an earlier one set ──
{
  const t0 = m.blankTurn('assistant');
  const t1 = m.apply(t0, { type: 'error', message: 'first', error_class: 'timeout.deadline_exceeded' });
  const t2 = m.apply(t1, { type: 'error', message: 'second' });
  assert(t2.error === 'second', 'the new message replaces the old one');
  assert(t2.errorClass === 'timeout.deadline_exceeded', 'errorClass is not wiped by a later event that omits it');
}

// ── version_mismatch is boolean-only, never a truthy string typo ──
{
  const t0 = m.blankTurn('assistant');
  const t1 = m.apply(t0, { type: 'error', message: 'x', version_mismatch: 'true' });
  assert(t1.versionMismatch === undefined, 'a non-boolean version_mismatch is ignored, not coerced');
  const t2 = m.apply(t0, { type: 'error', message: 'x', version_mismatch: true });
  assert(t2.versionMismatch === true, 'a real boolean version_mismatch is captured');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
