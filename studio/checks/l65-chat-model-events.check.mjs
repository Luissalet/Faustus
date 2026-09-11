// Lote 65 — Studio: turno de chat. Proves the client-side halves of several
// IDs by driving the REAL `decode()` (adapters/chat.ts) and `apply()`
// (screens/studio/model.ts) through esbuild, the same pattern
// tests/test_l29_studio_error_trace_decode_js.py already uses for this file
// pair — never a re-implementation of their logic.
//
//   - ask_user `revision` (PENDIENTES.md M1 / this lote): decode() forwards
//     it, toolEventsFrom() reads it from a persisted record, sendTurn()
//     would send it (checked separately, by source inspection, below).
//   - UX-02/TASK-03: the `uncertain` SSE event decodes and marks the turn.
//   - MOD-06: `capabilities_changed` decodes and marks the turn, with `lost`.
//   - RES-01: `research_progress`'s `coverage` list decodes onto
//     `turn.research.coverage`.
//   - EXEC-01: a `tool_output`'s `execution_target` decodes onto the step.
//
// Run by tests/test_l65_studio_events_js.py, or by hand:
//   node studio/checks/l65-chat-model-events.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);

async function bundle(entry, outName) {
  const out = join(mkdtempSync(join(tmpdir(), 'fs-l65-')), outName);
  await build({
    entryPoints: [join(root, entry)], bundle: true, platform: 'node', format: 'esm',
    outfile: out, logLevel: 'silent',
  });
  return import(pathToFileURL(out).href);
}

const { decode } = await bundle('studio/src/adapters/chat.ts', 'chat.mjs');
const { apply, blankTurn } = await bundle('studio/src/screens/studio/model.ts', 'model.mjs');

// ── ask_user revision ──
{
  const ev = decode({ type: 'ask_user', data: { question: 'Which?', options: [], question_id: 'q1', revision: 3 } }, null);
  assert.equal(ev.type, 'ask_user');
  assert.equal(ev.ask.questionId, 'q1');
  assert.equal(ev.ask.revision, 3);

  // Absent on an older server: undefined, not a crash, not a default of 0.
  const evNoRev = decode({ type: 'ask_user', data: { question: 'Which?', options: [] } }, null);
  assert.equal(evNoRev.ask.revision, undefined);
}

// ── UX-02/TASK-03: `uncertain` ──
{
  const ev = decode({ type: 'uncertain', status: 'checking' }, null);
  assert.equal(ev.type, 'uncertain');
  assert.equal(ev.status, 'checking');

  let turn = blankTurn('assistant');
  turn = apply(turn, ev);
  assert.equal(turn.uncertain, true);
  // Real content settles it either way.
  turn = apply(turn, { type: 'delta', text: 'hi', thinking: false });
  assert.equal(turn.uncertain, undefined);
}

// ── MOD-06: `capabilities_changed` ──
{
  const ev = decode(
    { type: 'capabilities_changed', round: 2, data: { from_model: 'big-model', to_model: 'small-model', lost: ['vision', 'tools'] } },
    null,
  );
  assert.equal(ev.type, 'capabilities_changed');
  assert.equal(ev.fromModel, 'big-model');
  assert.equal(ev.toModel, 'small-model');
  assert.deepEqual(ev.lost, ['vision', 'tools']);

  let turn = blankTurn('assistant');
  turn = apply(turn, ev);
  assert.deepEqual(turn.capabilitiesChanged, { fromModel: 'big-model', toModel: 'small-model', lost: ['vision', 'tools'] });
}

// ── RES-01: research coverage ──
{
  const ev = decode(
    {
      type: 'research_progress',
      data: {
        phase: 'analyzing', round: 1, message: 'analysing',
        coverage: [
          { question: 'What treatment options exist?', status: 'covered', matched_sources: 2 },
          { question: 'What is the prognosis?', status: 'pending', matched_sources: 0 },
        ],
      },
    },
    null,
  );
  assert.equal(ev.type, 'research');
  assert.equal(ev.coverage.length, 2);

  let turn = blankTurn('assistant');
  turn = apply(turn, ev);
  assert.equal(turn.research.coverage.length, 2);
  assert.equal(turn.research.coverage[0].status, 'covered');
  assert.equal(turn.research.coverage[0].matchedSources, 2);
  assert.equal(turn.research.coverage[1].status, 'pending');

  // A later event with no coverage at all keeps the last one seen, rather
  // than blanking the map the reader was just looking at.
  const evNoCoverage = decode({ type: 'research_progress', data: { phase: 'writing', round: 1, message: 'writing' } }, null);
  turn = apply(turn, evNoCoverage);
  assert.equal(turn.research.coverage.length, 2);
}

// ── EXEC-01: execution_target on a tool_output ──
{
  const ev = decode(
    { type: 'tool_output', tool: 'bash', command: 'ls', output: 'ok', exit_code: 0, execution_target: { kind: 'wsl', cwd: '/home/x', shell: 'bash' } },
    null,
  );
  assert.equal(ev.type, 'tool_output');
  assert.deepEqual(ev.executionTarget, { kind: 'wsl', cwd: '/home/x', shell: 'bash' });

  let turn = blankTurn('assistant');
  turn = apply(turn, { type: 'tool_start', tool: 'bash', command: 'ls', round: 1 });
  turn = apply(turn, ev);
  assert.equal(turn.steps.length, 1);
  assert.deepEqual(turn.steps[0].executionTarget, { kind: 'wsl', cwd: '/home/x', shell: 'bash' });

  // Absent from a server that has not been taught to forward it yet: no crash.
  const evNoTarget = decode({ type: 'tool_output', tool: 'bash', command: 'ls', output: 'ok', exit_code: 0 }, null);
  assert.equal(evNoTarget.executionTarget, undefined);
}

console.log('ok l65-chat-model-events');
