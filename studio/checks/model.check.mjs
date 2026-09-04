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
assert(r.steps[0].state === 'cancelled' && r.steps[0].meta === 'permiso respondido', 'resolved approval step is not waiting');
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

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
