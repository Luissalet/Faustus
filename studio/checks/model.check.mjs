// The Studio transcript reducer (screens/studio/model.ts), driven with a
// synthetic delegate_agents stream and a persisted history record — no
// model, no browser. Bundled with esbuild (a Vite dependency) on the fly;
// run by tests/test_studio_model_js.py, or by hand:
//   node studio/checks/model.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-model-')), 'model.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'screens', 'studio', 'model.ts')],
  bundle: true,
  format: 'esm',
  platform: 'node',
  outfile: out,
  logLevel: 'silent',
});
const m = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};
const ev = (payload) => ({ type: 'subagent', payload });

// ── A delegation, event by event ──
let t = m.blankTurn('assistant');
for (const p of [
  { event: 'queued', id: 'w1', delegation: 'd1', name: 'uno', index: 0, instruction: 'crea uno.txt', reason: 'gpu' },
  { event: 'queued', id: 'w2', delegation: 'd1', name: 'dos', index: 1, instruction: 'crea dos.txt' },
  { event: 'started', id: 'w1', delegation: 'd1', session_id: 'child-1', model: 'qwen3.5:9b', started_at: 1000 },
  { event: 'tool', id: 'w1', delegation: 'd1', tool: 'write_file', phase: 'start', command: 'uno.txt' },
  { event: 'tick', id: 'w1', delegation: 'd1', elapsed_s: 3, round: 1, idle_s: 0 },
  { event: 'tool', id: 'w1', delegation: 'd1', tool: 'write_file', phase: 'done', ok: true, output: 'Wrote 3 bytes' },
  { event: 'steer', id: 'w1', delegation: 'd1', text: 'usa mayúsculas', source: 'user' },
  { event: 'tick', id: 'w1', delegation: 'd1', elapsed_s: 9, stalled: true, stall_reason: 'no activity', idle_s: 6 },
  { event: 'done', id: 'w1', delegation: 'd1', stop_reason: 'complete', final_text: 'hecho', mutations: ['uno.txt'], tool_calls: 1, duration_s: 12, input_tokens: 1200, output_tokens: 80 },
  { event: 'started', id: 'w2', delegation: 'd1', session_id: 'child-2' },
  { event: 'error', id: 'w2', delegation: 'd1', message: 'se ha roto' },
]) t = m.apply(t, ev(p));
const [w1, w2] = t.workers;
assert(t.workers.length === 2, 'two workers, no duplicates');
assert(w1.status === 'done' && w1.finalText === 'hecho' && w1.mutations[0] === 'uno.txt', 'w1 done with final text and mutations');
assert(w1.toolCalls === 1 && w1.lastToolOk === true && w1.lastOut === 'Wrote 3 bytes', 'w1 tool folded');
assert(w1.steers.length === 1 && w1.steers[0].text === 'usa mayúsculas', 'steer line kept');
assert(w1.stalled === false, 'done clears stalled');
assert(w1.inTok === 1200 && w1.outTok === 80 && w1.durationS === 12, 'tokens and duration');
assert(w1.sessionId === 'child-1' && w1.model === 'qwen3.5:9b', 'session and model');
assert(w2.status === 'failed' && w2.error === 'se ha roto', 'w2 failed');
assert(!m.workerLive(w1) && !m.workerLive(w2), 'liveness');

// A worker still running when the stream ends is marked partial.
let t2 = m.blankTurn('assistant');
t2 = m.apply(t2, ev({ event: 'started', id: 'x', session_id: 'c' }));
t2 = m.apply(t2, { type: 'done' });
assert(t2.workers[0].status === 'partial', 'stream done → live worker partial');

// After an approval the server closes the parked call and repeats tool_start:
// one row, not two.
let t3 = m.blankTurn('assistant');
t3 = m.apply(t3, { type: 'tool_start', tool: 'write_file', command: 'a.txt', round: 1 });
t3 = m.apply(t3, { type: 'tool_output', tool: 'write_file', command: 'a.txt', output: '', exitCode: null });
t3 = m.apply(t3, { type: 'ask_user', ask: { question: 'Allow?', options: [], multi: false, kind: 'tool_approval', approvalId: 'a' } });
assert(t3.steps.length === 1 && t3.steps[0].state === 'waiting', 'ask_user marks the closed call as waiting');
t3 = m.apply(t3, { type: 'tool_start', tool: 'write_file', command: 'a.txt', round: 1 });
assert(t3.steps.length === 1 && t3.steps[0].state === 'running', 'the replayed tool_start reuses the waiting row');

// ── History restore ──
const meta = {
  tool_events: [
    { round: 1, tool: 'write_file', command: 'a.txt\nhola', output: 'Waiting for an exact user approval.', exit_code: null, ask_user: { kind: 'tool_approval', approval_id: 'ap', question: 'Allow?', options: [], resolved: 'approve' } },
    { round: 2, tool: 'write_file', command: 'a.txt\nhola', output: 'Wrote 4 bytes', exit_code: 0, diff: { text: '+++ b/a.txt\n@@ -0,0 +1 @@\n+hola', added: 1, removed: 0, new_file: true, file: 'a.txt' } },
    {
      round: 3,
      tool: 'delegate_agents',
      command: '{}',
      output: 'report',
      exit_code: 0,
      subagents: [
        { id: 'w1', name: 'uno', index: 0, session_id: 'c1', stop_reason: 'complete', tool_calls: 2, mutations: ['uno.txt'], final_text: 'listo', duration_s: 8 },
        { id: 'w2', name: 'dos', index: 1, session_id: 'c2', stop_reason: 'stopped', error: '' },
      ],
    },
  ],
  harness: { stop_reason: 'complete', mutations: ['a.txt', 'uno.txt'], tool_calls: 3, failed_calls: 0, checkpoint: 'abc', workspace: 'D:/x', review: { verdict: 'ok' } },
  web_sources: [{ title: 'Doc', url: 'https://x.y' }],
};
const r = m.restoreFromMetadata(m.blankTurn('assistant', 'texto'), meta);
assert(r.steps.length === 3, 'three steps restored');
assert(r.steps[0].state === 'cancelled' && r.steps[0].meta === 'permission answered', 'resolved approval step is not waiting');
assert(r.steps[1].diff && r.steps[1].diff.added === 1 && r.steps[1].diff.newFile, 'diff restored');
assert(r.workers.length === 2 && r.workers[0].status === 'done' && r.workers[1].status === 'stopped', 'workers restored from persisted records');
assert(r.summary && r.summary.mutations.length === 2 && r.summary.checkpoint === 'abc', 'harness summary restored');
assert(r.sources.length === 1 && r.ask === undefined && r.rounds === 3, 'sources, no pending ask, rounds');

const pending = m.restoreFromMetadata(m.blankTurn('assistant'), {
  tool_events: [{ round: 1, tool: 'bash', command: 'rm x', output: 'Waiting for an exact user approval.', exit_code: null, ask_user: { kind: 'tool_approval', approval_id: 'p1', question: 'Allow?', options: ['a'] } }],
});
assert(pending.ask && pending.ask.approvalId === 'p1' && pending.steps[0].state === 'waiting', 'unresolved approval comes back as a pending ask');

const plain = m.restoreFromMetadata(m.blankTurn('assistant', 'hola'), { model: 'x' });
assert(plain.steps.length === 0 && plain.summary === undefined, 'a chat turn restores nothing extra');

// ── The gate that was answered (06-09-2026, seen in the wild) ──
// The server appends the gate's question to the assistant's text, so when the
// turn stops there the WHOLE message is "Allow this task to continue?". Read
// back as prose, with the card gone because the gate is resolved, it is a
// question with no buttons under it: the exact shape of a chat that looks
// hung. The turn has to say the permission was answered instead.
{
  const answered = m.restoreFromMetadata(m.blankTurn('assistant', 'Allow this task to continue?'), {
    tool_events: [
      { round: 1, tool: 'read_file', command: 'docs/informe.md', output: 'ok', exit_code: 0 },
      {
        round: 6,
        tool: 'project_objectives',
        command: '{"action": "list"}',
        output: 'Waiting for an exact user approval.',
        exit_code: null,
        ask_user: { kind: 'tool_approval', approval_id: 'KD', question: 'Allow this task to continue?', options: [], resolved: 'approve_task' },
      },
    ],
    harness: { stop_reason: 'awaiting_user', tool_calls: 6, failed_calls: 1 },
  });
  assert(answered.ask === undefined, 'an answered gate is not a pending card');
  assert(answered.approval && answered.approval.decision === 'approve_task', 'the decision is kept as a record');
  assert(answered.text === '', 'the bare question does not come back as a bubble');
  assert(answered.steps.length === 2 && answered.steps[1].meta === 'permission answered', 'the rail still shows what happened');

  // The prose of a real answer is never thrown away, only the bare question.
  const withAnswer = m.restoreFromMetadata(m.blankTurn('assistant', 'Ya está: he listado los objetivos.'), {
    tool_events: [
      { round: 1, tool: 'project_objectives', command: '{}', output: 'Waiting for an exact user approval.', exit_code: null, ask_user: { kind: 'tool_approval', approval_id: 'KD', question: 'Allow this task to continue?', options: [], resolved: 'approve' } },
    ],
  });
  assert(withAnswer.text === 'Ya está: he listado los objetivos.', 'a real answer survives an answered gate');
  assert(withAnswer.approval.decision === 'approve', 'and the decision is still recorded');
}

// ── El latido: qué está haciendo el turno y a cuánto va ──
// La velocidad se mide por los huecos ENTRE los trozos que llegan, así que un
// buffer reproducido de golpe (reenganche a un run vivo) no puede inventarse
// 5.000 tok/s, y una espera de herramienta no puede hundirla a cero.
{
  let live = m.newLive(1000);
  assert(m.liveTps(live) === null, 'sin trozos todavia no hay velocidad que decir');

  // Cuarenta milisegundos por trozo = 25 tok/s.
  for (let i = 1; i <= 6; i++) live = m.liveToken(live, 1000 + i * 40, false);
  const tps = m.liveTps(live);
  assert(tps !== null && Math.abs(tps - 25) < 0.5, `40 ms por trozo se leen como 25 tok/s (leido: ${tps})`);
  assert(live.tokens === 6 && live.phase === 'writing', 'seis trozos, escribiendo');

  // Un buffer reproducido llega sin huecos: no cuenta como decodificacion.
  let replay = m.newLive(0);
  for (let i = 1; i <= 50; i++) replay = m.liveToken(replay, i, false);
  assert(replay.tokens === 50 && m.liveTps(replay) === null, 'un replay no inventa una velocidad');

  // Una espera larga (herramienta, prefill, cola) tampoco entra en la media.
  const before = m.liveTps(live);
  const afterGap = m.liveToken(live, live.lastTokenAt + 30_000, false);
  assert(Math.abs(m.liveTps(afterGap) - before) < 0.01, 'treinta segundos parado no cuentan como decodificar');

  // Las fases, y su reloj: cambia solo cuando cambia lo que hace.
  const thinking = m.liveToken(m.newLive(0), 100, true);
  assert(thinking.phase === 'thinking', 'un trozo de razonamiento es pensar, no escribir');
  const tool = m.livePhase(thinking, 500, 'tool', 'Read · a.md');
  assert(tool.phase === 'tool' && tool.label === 'Read · a.md' && tool.phaseAt === 500, 'la herramienta abre fase con su nombre');
  const same = m.livePhase(tool, 900, 'tool', 'Read · a.md');
  assert(same.phaseAt === 500 && same.lastAt === 900, 'la misma fase mantiene su reloj y renueva la senal de vida');
  const waiting = m.livePhase(same, 1200, 'waiting');
  assert(waiting.phase === 'waiting' && waiting.phaseAt === 1200, 'al acabar la herramienta se espera al modelo, con reloj nuevo');
}

// ── Y lo mismo, pero por los eventos de verdad ──
{
  const real = Date.now;
  let clock = 10_000;
  Date.now = () => clock;
  try {
    let t = m.blankTurn('assistant');
    assert(t.live && t.live.phase === 'waiting', 'un turno recien abierto ya esta esperando al modelo');
    clock += 500;
    t = m.apply(t, { type: 'tool_start', tool: 'read_file', command: 'a.md', round: 1 });
    assert(t.live.phase === 'tool' && t.live.label.startsWith('Read'), 'tool_start dice que herramienta corre');
    clock += 1200;
    t = m.apply(t, { type: 'tool_output', tool: 'read_file', command: 'a.md', output: 'ok', exitCode: 0 });
    assert(t.live.phase === 'waiting', 'con la herramienta hecha, la espera al modelo es la fase visible');
    for (let i = 0; i < 5; i++) {
      clock += 50;
      t = m.apply(t, { type: 'delta', text: 'x', thinking: false });
    }
    assert(t.live.phase === 'writing' && Math.abs(m.liveTps(t.live) - 20) < 0.5, 'los deltas dan 20 tok/s');
  } finally {
    Date.now = real;
  }
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
