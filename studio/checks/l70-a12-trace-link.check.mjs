// Lote 70a, punto A.12: `Transcript.tsx`'s tool card gets the "Ver traza"
// link (`/activity?trace=<call_id>&session=<sessionId>`) that
// `Activity.tsx::TracePanel` already reads — it just never had a call_id to
// offer. `src/agent_loop.py` already puts `call_id` on the wire's
// `tool_output` event and the persisted `tool_events[i]` entry; this checks
// `adapters/chat.ts`'s `decode()`/`toolEventsFrom()` both forward it onto
// `ChatEvent`/`HistoryToolEvent` as `callId`, plus a source check that
// `Transcript.tsx` actually renders the link off `step.callId`.
//
// Run by tests/test_l70_a12_trace_link_js.py, or by hand:
//   node studio/checks/l70-a12-trace-link.check.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-a12-trace-')), 'chat.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/chat.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
const { decode, toolEventsFrom } = await import(pathToFileURL(out).href);

let failed = 0;
const check = (cond, msg) => { if (!cond) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };

// ── decode(): live `tool_output` events carry call_id through as callId ──
{
  const ev = decode(
    { type: 'tool_output', tool: 'bash', command: 'ls', output: 'a.txt', exit_code: 0, call_id: 'call_abc123' },
    null,
  );
  check(ev.type === 'tool_output', 'decode() reads tool_output events');
  check(ev.callId === 'call_abc123', 'decode() forwards call_id as callId');
}
{
  const ev = decode({ type: 'tool_output', tool: 'bash', command: 'ls', output: '', exit_code: 0 }, null);
  check(ev.callId === undefined, 'decode() leaves callId undefined when the wire omits call_id (older server)');
}

// ── toolEventsFrom(): the persisted tool_events[i].call_id round-trips ──
{
  const events = toolEventsFrom({ tool_events: [
    { round: 1, tool: 'bash', command: 'ls', output: 'a.txt', exit_code: 0, call_id: 'call_xyz789' },
  ] });
  check(events.length === 1, 'toolEventsFrom() reads one persisted event');
  check(events[0].callId === 'call_xyz789', 'toolEventsFrom() forwards call_id as callId');
}
{
  const events = toolEventsFrom({ tool_events: [{ round: 1, tool: 'bash', command: 'ls', output: '', exit_code: 0 }] });
  check(events[0].callId === undefined, 'toolEventsFrom() leaves callId undefined when absent (history from before lote 70a)');
}

// ── Transcript.tsx: the "Ver traza" link is actually wired off step.callId ──
{
  const src = readFileSync(join(root, 'studio/src/screens/studio/Transcript.tsx'), 'utf-8');
  check(src.includes('step.callId'), 'Transcript.tsx reads step.callId');
  check(/\/activity\?trace=\$\{encodeURIComponent\(step\.callId\)\}/.test(src), 'Transcript.tsx links to /activity?trace=<call_id>');
  check(src.includes('data-testid="tool-trace-link"'), 'the trace link has a stable test id');
}

// ── model.ts: callId threads through both the live reducer and history restore ──
{
  const src = readFileSync(join(root, 'studio/src/screens/studio/model.ts'), 'utf-8');
  check(/callId:\s*event\.callId/.test(src), 'model.ts\'s tool_output reducer carries callId onto the Step');
  check(/callId:\s*ev\.callId/.test(src), 'model.ts\'s history restore carries callId onto the Step');
}

if (failed) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}
console.log('ALL OK');
