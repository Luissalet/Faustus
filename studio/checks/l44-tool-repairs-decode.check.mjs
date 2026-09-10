// L44 (closes an L39 gap): `decode()` (studio/src/adapters/chat.ts) forwards
// a `tool_output` event's wire `argument_errors`/`repairs`
// (`_validate_native_tool_call`, src/agent_loop.py) onto the `ChatEvent` as
// `argumentErrors`/`repairs` — the one change
// studio/checks/l39-tool-repairs.check.mjs's own comments said was still
// needed for the LIVE path (history restore already worked off the
// persisted `tool_events[i]` entry via `restoreFromMetadata`).
//
// This drives BOTH real modules together (not a re-implementation of
// either): `decode()` turns a raw wire payload into a `ChatEvent`, then
// `model.ts`'s real `apply()` folds that event onto a `Step` — proving the
// two lotes' work actually connects end to end, not just that each one's
// own half is individually plausible.
//
// Run by tests/test_l44_tool_repairs_decode_js.py, or by hand:
//   node studio/checks/l44-tool-repairs-decode.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-l44-tool-repairs-decode-'));

const chatOut = join(dir, 'chat.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/chat.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: chatOut, logLevel: 'silent' });
const { decode } = await import(pathToFileURL(chatOut).href);

const modelOut = join(dir, 'model.mjs');
await build({
  entryPoints: [join(root, 'studio/src/screens/studio/model.ts')],
  bundle: true, platform: 'node', format: 'esm', outfile: modelOut, logLevel: 'silent',
  jsx: 'automatic',
});
const { blankTurn, apply } = await import(pathToFileURL(modelOut).href);

let failed = 0;
const check = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else {
    console.log('ok', message);
  }
};

// ── decode() alone: the wire's snake_case forwards to camelCase ──
{
  const ev = decode({
    type: 'tool_output', tool: 'read_file', command: 'read_file {"path": "x.py", "limit": "90"}',
    output: 'ok', exit_code: 0,
    argument_errors: [{ field: 'limit', kind: 'wrong_type', detail: 'expected integer, got string' }],
    repairs: [{ field: 'limit', from: '90', to: 90, reason: 'numeric string for an integer field' }],
  }, null);
  check(ev.type === 'tool_output', 'decode() recognises the tool_output type');
  check(Array.isArray(ev.argumentErrors) && ev.argumentErrors.length === 1, 'argument_errors -> argumentErrors');
  check(ev.argumentErrors[0].kind === 'wrong_type', 'error fields preserved');
  check(Array.isArray(ev.repairs) && ev.repairs.length === 1, 'repairs forwarded');
  check(ev.repairs[0].to === 90, 'repair "to" keeps its real (numeric) type, not stringified');
}

// ── decode() alone: an event with NEITHER field decodes exactly as before ──
{
  const ev = decode({ type: 'tool_output', tool: 'bash', command: 'ls', output: 'a.py', exit_code: 0 }, null);
  check(ev.argumentErrors === undefined, 'no capability lost: absent argument_errors stays undefined');
  check(ev.repairs === undefined, 'no capability lost: absent repairs stays undefined');
}

// ── decode() alone: a blocking error with no repair at all (nothing fixable) ──
{
  const ev = decode({
    type: 'tool_output', tool: 'edit_file', command: 'edit_file {...}', output: 'blocked', exit_code: 1,
    argument_errors: [{ field: 'path', kind: 'path_scope', detail: 'outside the workspace' }],
  }, null);
  check(ev.argumentErrors.length === 1, 'the unresolved error is present');
  check(ev.repairs === undefined, 'no repairs array when nothing was repaired');
}

// ── end to end: decode() -> apply() lands the repair on the live Step ──
{
  let turn = blankTurn('assistant');
  turn = apply(turn, decode({ type: 'tool_start', tool: 'edit_file', command: 'edit_file c', round: 1 }, null));
  turn = apply(turn, decode({
    type: 'tool_output', tool: 'edit_file', command: 'edit_file c', output: 'ok', exit_code: 0,
    argument_errors: [{ field: 'limit', kind: 'wrong_type', detail: 'expected integer' }],
    repairs: [{ field: 'limit', from: '5', to: 5, reason: 'numeric string' }],
  }, null));
  check(turn.steps.length === 1, 'the live step exists');
  check(turn.steps[0].repairs?.[0]?.to === 5, 'a real wire event, decoded and applied, lands the repair on the step');
  check(turn.steps[0].argumentErrors?.[0]?.kind === 'wrong_type', 'and the argument error alongside it');
}

// ── end to end: an ordinary tool_output (no repairs) still applies cleanly ──
{
  let turn = blankTurn('assistant');
  turn = apply(turn, decode({ type: 'tool_start', tool: 'bash', command: 'pytest', round: 1 }, null));
  turn = apply(turn, decode({ type: 'tool_output', tool: 'bash', command: 'pytest', output: 'ok', exit_code: 0 }, null));
  check(turn.steps.length === 1, 'the live step was created');
  check(turn.steps[0].argumentErrors === undefined && turn.steps[0].repairs === undefined,
    'no phantom fields on a clean call decoded through the real pipeline');
}

if (failed) {
  console.error(`${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('ALL OK');
