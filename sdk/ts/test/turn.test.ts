import { test } from 'node:test';
import assert from 'node:assert/strict';
import { FaustusClient } from '../src/client.js';
import { RunNotActiveError } from '../src/errors.js';
import {
  breakingStream,
  emptyResponse,
  fakeFetch,
  headerValue,
  jsonResponse,
  SSE_DONE,
  sseFrame,
  stallingStream,
  streamResponse,
  textStream,
} from './helpers.js';

test('Turn: a dropped connection reconnects by cursor, deduplicates the overlap, and finishes on [DONE]', async () => {
  const { fetch, calls } = fakeFetch((call, i) => {
    if (i === 0) {
      assert.equal(call.method, 'POST');
      assert.ok(call.url.endsWith('/api/chat_stream'));
      return streamResponse(
        breakingStream([sseFrame({ type: 'agent_step', round: 1, sequence: 1 }), sseFrame({ delta: 'hi', sequence: 2 })]),
        { headers: { 'X-Faustus-Run-Id': 'run-1' } },
      );
    }
    if (i === 1) {
      assert.equal(call.method, 'GET');
      assert.ok(call.url.includes('/api/chat/resume/s1'));
      assert.ok(call.url.includes('cursor=2'), call.url);
      return streamResponse(
        textStream([
          sseFrame({ type: 'agent_step', round: 1, sequence: 2 }), // overlap: must be skipped
          sseFrame({ delta: 'again', sequence: 3 }),
          SSE_DONE,
        ]),
        { headers: { 'X-Faustus-Run-Id': 'run-1' } },
      );
    }
    throw new Error(`unexpected call ${i}: ${call.url}`);
  });

  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const turn = await client.turns.create('s1', { message: 'hi', mode: 'chat' });
  assert.equal(turn.runId, 'run-1');

  const events = [];
  for await (const ev of turn) events.push(ev);

  assert.deepEqual(
    events.map((e) => e.type),
    ['agent_step', 'delta', 'delta'],
  );
  const end = await turn.done;
  assert.equal(end.reason, 'done');
  assert.equal(end.lastSequence, 3);
  assert.equal(calls.length, 2);
});

test('Turn: 404 on resume ends the turn with reason "run_gone"', async () => {
  const { fetch, calls } = fakeFetch((_call, i) => {
    if (i === 0) return streamResponse(breakingStream([]));
    if (i === 1) return emptyResponse(404);
    throw new Error('unexpected extra call');
  });
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const turn = await client.turns.create('s1', { message: 'hi', mode: 'chat' }, { maxResumes: 3 });
  const end = await turn.done;
  assert.equal(end.reason, 'run_gone');
  assert.equal(calls.length, 2);
});

test('Turn: resume attempts exhausted ends the turn with reason "error"', async () => {
  const { fetch, calls } = fakeFetch((_call, i) => {
    // Every attempt (the original stream, and the one resume this test
    // allows) breaks before [DONE].
    return streamResponse(breakingStream([]));
  });
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const turn = await client.turns.create('s1', { message: 'hi', mode: 'chat' }, { maxResumes: 1 });
  const end = await turn.done;
  assert.equal(end.reason, 'error');
  // The original POST plus exactly one resume attempt — never a third.
  assert.equal(calls.length, 2);
});

test('Turn: silence past idleTimeoutMs is treated as a broken connection and reconnects', async () => {
  const { fetch, calls } = fakeFetch((_call, i) => {
    if (i === 0) return streamResponse(stallingStream([sseFrame({ type: 'agent_step', round: 1, sequence: 1 })]));
    if (i === 1) return streamResponse(textStream([SSE_DONE]));
    throw new Error('unexpected extra call');
  });
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const turn = await client.turns.create('s1', { message: 'hi', mode: 'chat' }, { idleTimeoutMs: 40, maxResumes: 3 });
  const end = await turn.done;
  assert.equal(end.reason, 'done');
  assert.equal(calls.length, 2);
  assert.ok(calls[1]?.url.includes('cursor=1'));
});

test('Turn.cancel(): sends X-Faustus-Run-Id and a JSON {scope} body, and never repeats the POST', async () => {
  const { fetch, calls } = fakeFetch((call, i) => {
    if (i === 0) {
      return streamResponse(textStream([SSE_DONE]), { headers: { 'X-Faustus-Run-Id': 'run-xyz' } });
    }
    if (i === 1) {
      assert.equal(call.method, 'POST');
      assert.ok(call.url.endsWith('/api/chat/stop/s1'));
      assert.equal(headerValue(call.init, 'X-Faustus-Run-Id'), 'run-xyz');
      assert.equal(headerValue(call.init, 'Content-Type'), 'application/json');
      assert.deepEqual(JSON.parse(String(call.init.body)), { scope: 'task' });
      return jsonResponse({ scope: 'task', stopped: true, cancelled_at: 123, what_ran_before: [], cleanup: {} });
    }
    throw new Error('unexpected extra call');
  });
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const turn = await client.turns.create('s1', { message: 'hi', mode: 'chat' });
  await turn.done;
  const result = await turn.cancel('task');
  assert.equal(result.stopped, true);
  assert.equal(result.scope, 'task');
  assert.equal(calls.length, 2, 'cancel() must never re-issue the chat_stream POST');
});

test('client.turns.stop(): standalone stop with an explicit runId', async () => {
  const { fetch, calls } = fakeFetch((call) => {
    assert.equal(headerValue(call.init, 'X-Faustus-Run-Id'), 'run-abc');
    return jsonResponse({ stopped: true });
  });
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  const result = await client.turns.stop('s1', { runId: 'run-abc' });
  assert.equal(result.stopped, true);
  assert.equal(calls.length, 1);
});

test('client.turns.resume(): a 404 throws RunNotActiveError, not a silent empty turn', async () => {
  const { fetch } = fakeFetch(() => emptyResponse(404));
  const client = new FaustusClient({ baseUrl: 'http://sdk-test', fetch });
  await assert.rejects(() => client.turns.resume('s1'), RunNotActiveError);
});
