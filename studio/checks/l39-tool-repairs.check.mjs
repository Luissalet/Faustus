// CALL-03 (Lote 39): the tool card's "original → corrección". Drives the
// real `restoreFromMetadata`/`apply` (studio/src/screens/studio/model.ts,
// bundled with esbuild, not a re-implementation) proving:
//   - a persisted `tool_events[i]` entry carrying `argument_errors`/
//     `repairs` (src/agent_loop.py's `_validate_native_tool_call` shape)
//     lands on the matching `Step.argumentErrors`/`Step.repairs` on
//     history restore;
//   - a persisted entry with NEITHER field (every record before CALL-03,
//     and every tool call that validated cleanly) restores exactly as it
//     always did — both fields simply absent, nothing else disturbed;
//   - the live `apply()` reducer's `tool_output` case reads the same
//     fields defensively off the event (ready for the day `chat.ts`'s
//     `decode()` forwards them — see this lote's report) without breaking
//     on an event that does not carry them at all.
//
// Run by tests/test_studio_l39_tool_repairs_js.py, or by hand:
//   node studio/checks/l39-tool-repairs.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-l39-tool-repairs-')), 'model.mjs');
await build({
  entryPoints: [join(root, 'studio/src/screens/studio/model.ts')],
  bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent',
  jsx: 'automatic',
});
const { restoreFromMetadata, blankTurn, apply } = await import(pathToFileURL(out).href);

let failed = 0;
const check = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else {
    console.log('ok', message);
  }
};

// ── history restore: a repaired call shows original → corrección ──
{
  const meta = {
    tool_events: [
      {
        round: 1, tool: 'read_file', command: 'read_file {"path": "x.py", "limit": "90"}',
        output: 'ok', exit_code: 0,
        argument_errors: [{ field: 'limit', kind: 'wrong_type', detail: 'expected integer, got string' }],
        repairs: [{ field: 'limit', from: '90', to: 90, reason: 'numeric string for an integer field' }],
      },
    ],
  };
  const turn = restoreFromMetadata(blankTurn('assistant'), meta);
  check(turn.steps.length === 1, 'one step restored');
  const step = turn.steps[0];
  check(Array.isArray(step.repairs) && step.repairs.length === 1, 'the repair made it onto the step');
  check(step.repairs[0].field === 'limit' && step.repairs[0].from === '90' && step.repairs[0].to === 90, 'repair fields match the persisted entry exactly');
  check(Array.isArray(step.argumentErrors) && step.argumentErrors.length === 1, 'the full error list made it onto the step too');
  check(step.argumentErrors[0].kind === 'wrong_type', 'error kind preserved');
}

// ── history restore: an error the repair could NOT resolve, no repairs at all ──
{
  const meta = {
    tool_events: [
      {
        round: 1, tool: 'edit_file', command: 'edit_file {...}', output: 'blocked', exit_code: 1,
        argument_errors: [{ field: 'path', kind: 'path_scope', detail: 'outside the workspace' }],
      },
    ],
  };
  const turn = restoreFromMetadata(blankTurn('assistant'), meta);
  const step = turn.steps[0];
  check(Array.isArray(step.argumentErrors) && step.argumentErrors.length === 1, 'the unresolved error is present');
  check(step.repairs === undefined, 'no repairs array when nothing was repaired (never an empty array)');
}

// ── compatibility: a tool_events entry with neither field restores exactly as before ──
{
  const meta = {
    tool_events: [
      { round: 1, tool: 'bash', command: 'ls', output: 'a.py\nb.py', exit_code: 0 },
    ],
  };
  const turn = restoreFromMetadata(blankTurn('assistant'), meta);
  const step = turn.steps[0];
  check(step.argumentErrors === undefined, 'no capability lost: an old-shape record leaves argumentErrors undefined');
  check(step.repairs === undefined, 'no capability lost: an old-shape record leaves repairs undefined');
  check(step.command === 'ls' && step.output === 'a.py\nb.py', 'the rest of the step restores unchanged');
}

// ── multiple tool calls: repairs line up with the RIGHT call, not the first ──
{
  const meta = {
    tool_events: [
      { round: 1, tool: 'read_file', command: 'read_file a', output: 'ok', exit_code: 0 },
      {
        round: 1, tool: 'edit_file', command: 'edit_file b', output: 'ok', exit_code: 0,
        repairs: [{ field: 'dry_run', from: 'true', to: true, reason: 'boolean string' }],
      },
    ],
  };
  const turn = restoreFromMetadata(blankTurn('assistant'), meta);
  check(turn.steps.length === 2, 'both calls restored');
  check(turn.steps[0].repairs === undefined, 'the first call (no repair) stays clean');
  check(turn.steps[1].repairs?.[0]?.field === 'dry_run', 'the repair landed on the SECOND call, not the first');
}

// ── live apply(): a tool_output event with no argument_errors/repairs
//    (today's decode() shape) applies exactly as before — no crash, no
//    phantom fields ──
{
  let turn = blankTurn('assistant');
  turn = apply(turn, { type: 'tool_start', tool: 'bash', command: 'pytest', round: 1 });
  turn = apply(turn, { type: 'tool_output', tool: 'bash', command: 'pytest', output: 'ok', exitCode: 0 });
  check(turn.steps.length === 1, 'the live step was created');
  check(turn.steps[0].argumentErrors === undefined && turn.steps[0].repairs === undefined, 'absent on a ChatEvent that does not carry them (decode() does not forward them yet)');
}

// ── live apply(): the SAME reducer, fed an event that DOES carry the raw
//    fields (what decode() would produce once it forwards them — see the
//    report), already picks them up — proving the passthrough is wired,
//    not just declared ──
{
  let turn = blankTurn('assistant');
  turn = apply(turn, { type: 'tool_start', tool: 'edit_file', command: 'edit_file c', round: 1 });
  turn = apply(turn, {
    type: 'tool_output', tool: 'edit_file', command: 'edit_file c', output: 'ok', exitCode: 0,
    argumentErrors: [{ field: 'limit', kind: 'wrong_type', detail: 'expected integer' }],
    repairs: [{ field: 'limit', from: '5', to: 5, reason: 'numeric string' }],
  });
  check(turn.steps[0].repairs?.[0]?.to === 5, 'apply() already reads a widened tool_output event correctly, no further change needed here once chat.ts forwards it');
}

if (failed) {
  console.error(`${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('ALL OK');
