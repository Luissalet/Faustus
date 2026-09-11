// Lote 69b — OBS-01: the trace adapter (studio/src/adapters/observability.ts)
// parses `GET /api/observability/trace/{call_id}`'s wire shape (see
// tests/test_l69b_obs01_trace_wire.py for the Python side of this same
// contract) into the camelCase shape Activity.tsx's TracePanel renders, and
// `traceForCall` appends `session_id` to the URL only when given one.
//
// Run by tests/test_l69b_studio_checks_js.py, or by hand:
//   node studio/checks/l69b-obs01-trace.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';

async function bundle(entry) {
  const result = await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', write: false, logLevel: 'silent' });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

const { callTraceFrom, traceForCall } = await bundle('studio/src/adapters/observability.ts');

let failed = 0;
const check = (condition, message) => {
  if (!condition) { failed += 1; console.error('FAIL:', message); }
  else console.log('ok', message);
};

// ── callTraceFrom(): pure parsing of the wire shape ──
{
  const raw = {
    call_id: 'call_1',
    events: [{ type: 'tool_start', tool: 'bash', round: 2, call_id: 'call_1', command: 'ls' }],
    artifacts: [{
      occurrence_id: 'occ_1', manifest_id: 'man_1', version: 3, state: 'final',
      format: 'png', byte_size: 1024, sha256: 'abc123', generator: 'render',
      created_at: '2026-01-01T00:00:00Z', label: 'cover.png', kind: 'image', owner: 'session_1',
    }],
    receipt: { decision: 'allowed', call_id: 'call_1' },
    found: true,
  };
  const trace = callTraceFrom(raw);
  check(trace.callId === 'call_1', 'call_id → callId');
  check(trace.found === true, 'found carries through');
  check(trace.events.length === 1 && trace.events[0].type === 'tool_start', 'event type parses');
  check(trace.events[0].tool === 'bash', 'event tool parses');
  check(trace.events[0].round === 2, 'event round parses as a number');
  check(trace.events[0].raw.command === 'ls', 'the raw payload is kept verbatim, not reshaped');
  const a = trace.artifacts[0];
  check(a.occurrenceId === 'occ_1' && a.manifestId === 'man_1', 'artifact ids parse');
  check(a.version === 3 && a.byteSize === 1024, 'artifact numeric fields parse as numbers');
  check(a.sha256 === 'abc123' && a.generator === 'render', 'artifact identity fields parse');
  check(a.label === 'cover.png' && a.kind === 'image' && a.owner === 'session_1', 'occurrence-enriched fields parse');
  check(trace.receipt && trace.receipt.decision === 'allowed', 'receipt is kept as an opaque object');
}

// ── a not-found trace: no events/artifacts/receipt, found: false ──
{
  const trace = callTraceFrom({ call_id: 'nope', events: [], artifacts: [], receipt: null, found: false });
  check(trace.found === false, 'not-found stays false, never guessed true');
  check(trace.events.length === 0 && trace.artifacts.length === 0, 'empty lists stay empty');
  check(trace.receipt === null, 'a null receipt parses as null, not {}');
}

// ── traceForCall(): URL shape, session_id only appended when given ──
{
  let calledUrl = null;
  globalThis.fetch = async (url) => {
    calledUrl = String(url);
    return new Response(JSON.stringify({ call_id: 'call_1', events: [], artifacts: [], receipt: null, found: false }), { status: 200 });
  };
  await traceForCall('call_1');
  check(calledUrl === '/api/observability/trace/call_1', 'no session_id → no query string at all');

  await traceForCall('call_1', 'sid-a');
  check(calledUrl === '/api/observability/trace/call_1?session_id=sid-a', 'a session_id is appended as a query param');

  await traceForCall('call with spaces');
  check(calledUrl === '/api/observability/trace/call%20with%20spaces', 'the call_id itself is URL-encoded');
}

if (failed) {
  console.error(`${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('ALL OK: observability.ts parses trace_for_call\'s wire shape and builds the right URL');
