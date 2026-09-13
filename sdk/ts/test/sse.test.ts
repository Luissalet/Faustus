import { test } from 'node:test';
import assert from 'node:assert/strict';
import { decode, pumpBody, type SseEvent } from '../src/sse.js';
import { SSE_DONE, sseFrame, textStream } from './helpers.js';

async function collect(stream: ReadableStream<Uint8Array>): Promise<SseEvent[]> {
  const events: SseEvent[] = [];
  const outcome = await pumpBody(stream, (raw, sseEvent) => {
    const ev = decode(raw, sseEvent);
    if (ev) events.push(ev);
  }, { idleTimeoutMs: 5_000 });
  assert.equal(outcome, 'done');
  return events;
}

test('decode: delta frame has no `type` and normalizes to type "delta"', () => {
  const ev = decode({ delta: 'hello', sequence: 1 }, null);
  assert.deepEqual(ev, {
    type: 'delta',
    delta: 'hello',
    thinking: false,
    sequence: 1,
    trace_id: undefined,
    step_id: undefined,
    stream_id: undefined,
    schema_version: undefined,
  });
});

test('decode: thinking delta', () => {
  const ev = decode({ delta: 'reasoning...', thinking: true }, null);
  assert.equal(ev?.type, 'delta');
  assert.equal((ev as { thinking: boolean }).thinking, true);
});

test('decode: event: error with a body that carries no `type`', () => {
  const ev = decode({ text: 'boom' }, 'error');
  assert.deepEqual(ev, {
    type: 'error',
    text: 'boom',
    error: undefined,
    message: undefined,
    detail: undefined,
    error_class: undefined,
    sequence: undefined,
    trace_id: undefined,
    step_id: undefined,
    stream_id: undefined,
    schema_version: undefined,
  });
});

test('decode: a core event by its own `type` field (no preceding event: line)', () => {
  const ev = decode({ type: 'agent_step', round: 3, sequence: 9 }, null);
  assert.deepEqual(ev, { type: 'agent_step', round: 3, sequence: 9, trace_id: undefined, step_id: undefined, stream_id: undefined, schema_version: undefined });
});

test('decode: an extended/unknown type forwards raw fields as UnknownEvent', () => {
  const ev = decode({ type: 'paused', anything: 42 }, null);
  assert.equal(ev?.type, 'paused');
  assert.equal((ev as Record<string, unknown>).anything, 42);
});

test('decode: a frame with neither `type` nor `delta` is skipped', () => {
  assert.equal(decode({ sequence: 1 }, null), null);
});

test('pumpBody: [DONE] terminates the stream', async () => {
  const body = textStream([sseFrame({ delta: 'hi' }), SSE_DONE]);
  const events = await collect(body);
  assert.equal(events.length, 1);
  assert.equal(events[0]?.type, 'delta');
});

test('pumpBody: frames split arbitrarily across chunk boundaries', async () => {
  const whole = sseFrame({ type: 'agent_step', round: 1 }) + sseFrame({ delta: 'ab' }) + sseFrame({ delta: 'cd' }) + SSE_DONE;
  // Chop into 3-byte pieces — a frame, a line, even a single `\n` can land
  // on either side of a chunk boundary.
  const chunks: string[] = [];
  for (let i = 0; i < whole.length; i += 3) chunks.push(whole.slice(i, i + 3));
  const events = await collect(textStream(chunks));
  assert.deepEqual(
    events.map((e) => e.type),
    ['agent_step', 'delta', 'delta'],
  );
});

test('pumpBody: a named event: line followed by a typed body', async () => {
  const body = textStream([sseFrame({ type: 'git_policy', action: 'commit', ok: true, sha: 'abc123' }, 'git_policy'), SSE_DONE]);
  const events = await collect(body);
  assert.deepEqual(events[0], {
    type: 'git_policy',
    action: 'commit',
    ok: true,
    branch: undefined,
    sha: 'abc123',
    detail: undefined,
    sequence: undefined,
    trace_id: undefined,
    step_id: undefined,
    stream_id: undefined,
    schema_version: undefined,
  });
});

test('pumpBody: an unparsable data: line is skipped, not fatal', async () => {
  const body = textStream(['data: {not json\n\n', sseFrame({ delta: 'ok' }), SSE_DONE]);
  const events = await collect(body);
  assert.equal(events.length, 1);
  assert.equal(events[0]?.type, 'delta');
});

test('pumpBody: throws when the stream ends without [DONE]', async () => {
  const body = textStream([sseFrame({ delta: 'partial' })]);
  await assert.rejects(() => pumpBody(body, () => {}, { idleTimeoutMs: 5_000 }));
});
