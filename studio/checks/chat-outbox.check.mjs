// client_message_id outbox (UX-02/TASK-03): a send writes a localStorage
// entry before the fetch and clears it the moment a response comes back —
// regardless of what the stream itself says — so a reload between the POST
// and its response is the only case that finds anything left to retry, and
// a retry always reuses the SAME id.
//
// Run by tests/test_chat_idempotency.py (node studio/checks/chat-outbox.check.mjs),
// or by hand:
//   node studio/checks/chat-outbox.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const out = join(mkdtempSync(join(tmpdir(), 'faustus-chat-outbox-')), 'chat.mjs');
await build({ entryPoints: ['studio/src/adapters/chat.ts'], bundle: true, platform: 'node', format: 'esm', outfile: out });

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

const { sendTurn, pendingOutboxFor, clearOutboxFor } = await import(pathToFileURL(out).href);

// 1) A send that gets a response — success or a mid-stream error — leaves no
// pending outbox entry: the server saw it, so there is nothing left to retry.
{
  store.clear();
  globalThis.fetch = async () => new Response('data: [DONE]\n\n', { headers: { 'X-Odysseus-Run-Id': 'run-1' } });
  for await (const _event of sendTurn({ sessionId: 's1', message: 'hello', mode: 'chat' })) {
    // drain
  }
  assert.equal(pendingOutboxFor('s1'), null, 'a completed send must leave no pending outbox entry');
}

// 2) A send whose fetch never resolves at all (dropped connection, reload
// mid-flight) leaves the outbox entry standing — the case worth retrying.
{
  store.clear();
  globalThis.fetch = async () => {
    throw new TypeError('Failed to fetch'); // never got a response
  };
  try {
    for await (const _event of sendTurn({ sessionId: 's2', message: 'unsent', mode: 'chat' })) {
      // drain
    }
    assert.fail('sendTurn must propagate the fetch failure');
  } catch {
    // expected — the caller (Studio.tsx) is what turns this into a bubble
  }
  const pending = pendingOutboxFor('s2');
  assert.ok(pending, 'a send whose fetch never answered must stay pending');
  assert.equal(pending.text, 'unsent');
  assert.equal(typeof pending.id, 'string');
  assert.ok(pending.id.length > 0);
}

// 3) A deliberate Stop (AbortController.abort) during that same kind of
// failure must NOT leave anything to retry — cancelling on purpose is not
// "uncertain", and reconciling it back would resurrect a stopped turn.
{
  store.clear();
  const controller = new AbortController();
  globalThis.fetch = async () => {
    controller.abort();
    const err = new DOMException('aborted', 'AbortError');
    throw err;
  };
  try {
    for await (const _event of sendTurn({ sessionId: 's3', message: 'stopped', mode: 'chat', signal: controller.signal })) {
      // drain
    }
  } catch {
    /* expected */
  }
  assert.equal(pendingOutboxFor('s3'), null, 'an aborted-on-purpose send must not be retried later');
}

// 4) A retry (reconcileOutbox in Studio.tsx) MUST reuse the pending entry's
// id — never mint a new one — or the server has no way to recognise it as
// the same turn.
{
  store.clear();
  let capturedId;
  globalThis.fetch = async () => new Response('data: [DONE]\n\n');
  globalThis.fetch = async () => {
    throw new TypeError('offline');
  };
  try {
    for await (const _event of sendTurn({ sessionId: 's4', message: 'retry me', mode: 'chat' })) {
      // drain
    }
  } catch {
    /* expected: leaves an entry pending */
  }
  const pending = pendingOutboxFor('s4');
  assert.ok(pending);
  globalThis.fetch = async (_url, options) => {
    capturedId = options.body.get('client_message_id');
    return new Response('data: [DONE]\n\n');
  };
  for await (const _event of sendTurn({
    sessionId: 's4',
    message: pending.text,
    mode: 'chat',
    clientMessageId: pending.id,
  })) {
    // drain
  }
  assert.equal(capturedId, pending.id, 'a retry must send the SAME client_message_id back');
  assert.equal(pendingOutboxFor('s4'), null);
}

// 5) client_message_id is optional and additive: it changes nothing about
// the request unless something reads it.
{
  store.clear();
  let sawKey = true;
  globalThis.fetch = async (_url, options) => {
    sawKey = options.body.has('client_message_id');
    return new Response('data: [DONE]\n\n');
  };
  for await (const _event of sendTurn({ sessionId: 's5', message: 'hi', mode: 'chat', approval: { id: 'a1', decision: 'approve' } })) {
    // drain
  }
  assert.equal(sawKey, false, 'an approval continuation is not a new outbox-tracked message');
  clearOutboxFor('s5');
}

console.log('client_message_id outbox: all checks passed');
