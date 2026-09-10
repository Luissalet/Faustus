// L29 (integrates L28's "necessary in adapters/chat.ts" note): `decode()`
// forwards `error_class`/`trace_id`/`step_id`/`version_mismatch` onto the
// `error`/`terminal` ChatEvent as `errorClass`/`traceId`/`stepId`/
// `versionMismatch` — the OBS-01/OBS-03/ARCH-01 fields `agent_runs.py`'s
// `_observability_fields` and `llm_core.py`'s `_stream_error_chunk` already
// put on the wire, which `errorTaxonomy.ts`'s `describeError`/`friendlyError`
// (Transcript.tsx) and model.ts's own `errorTraceFields()` passthrough are
// both ready to consume — this proves the missing half, decode() itself,
// actually emits them, additively (an event missing a field decodes exactly
// as it always did).
//
// Run by tests/test_l29_studio_error_trace_decode_js.py, or by hand:
//   node studio/checks/l29-error-trace-decode.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-errtrace-decode-')), 'chat.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/chat.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
const { decode } = await import(pathToFileURL(out).href);

// ── an `event: error` SSE chunk (sseEvent === 'error' branch) ──
{
  const ev = decode(
    { text: 'llm down', error_class: 'transport.llm_service_error', trace_id: 'tr_1', step_id: 'st_2' },
    'error',
  );
  assert.equal(ev.type, 'error');
  assert.equal(ev.message, 'llm down');
  assert.equal(ev.errorClass, 'transport.llm_service_error');
  assert.equal(ev.traceId, 'tr_1');
  assert.equal(ev.stepId, 'st_2');
}

// ── a `data: {type: "error", ...}` payload (the switch's 'error' case) ──
{
  const ev = decode(
    { type: 'error', message: 'boom', error_class: 'timeout.deadline_exceeded', trace_id: 'tr_3', step_id: 'st_4' },
    null,
  );
  assert.equal(ev.type, 'error');
  assert.equal(ev.errorClass, 'timeout.deadline_exceeded');
  assert.equal(ev.traceId, 'tr_3');
  assert.equal(ev.stepId, 'st_4');
}

// ── absent fields decode exactly as before this lote (no capability lost) ──
{
  const ev = decode({ text: 'plain failure' }, 'error');
  assert.equal(ev.message, 'plain failure');
  assert.equal(ev.errorClass, undefined);
  assert.equal(ev.traceId, undefined);
  assert.equal(ev.stepId, undefined);
}

// ── agent_terminal: error_class may be nested under `data.failure` ──
{
  const ev = decode(
    { type: 'agent_terminal', data: { failure: { message: 'model crashed', error_class: 'unknown.panic' } },
      trace_id: 'tr_5', step_id: 'st_6' },
    null,
  );
  assert.equal(ev.type, 'terminal');
  assert.equal(ev.failed, true);
  assert.equal(ev.message, 'model crashed');
  assert.equal(ev.errorClass, 'unknown.panic');
  assert.equal(ev.traceId, 'tr_5');
  assert.equal(ev.stepId, 'st_6');
}

// ── a successful chat_terminal never fabricates an errorClass ──
{
  const ev = decode({ type: 'chat_terminal' }, null);
  assert.equal(ev.type, 'terminal');
  assert.equal(ev.failed, false);
  assert.equal(ev.errorClass, undefined);
}

console.log('ok l29-error-trace-decode');
