// TASK-04/UX-04: the Stop-menu adapters — stopChat's optional `scope`,
// pauseChat, and steerChat's `mode`. Each is a thin fetch wrapper, so this
// checks the exact request shape sent for every scope/mode AND that the
// original (no-scope) stopChat request is byte-for-byte what it always was —
// a stale client that only ever calls stopChat(sid, runId) must keep working.
//
// Run by tests/test_stop_menu_adapters.py (node studio/checks/stop-menu.check.mjs),
// or by hand:
//   node studio/checks/stop-menu.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const out = join(mkdtempSync(join(tmpdir(), 'faustus-stop-menu-')), 'chat.mjs');
await build({ entryPoints: ['studio/src/adapters/chat.ts'], bundle: true, platform: 'node', format: 'esm', outfile: out });

const { stopChat, pauseChat, steerChat } = await import(pathToFileURL(out).href);

// 1) No scope: the ORIGINAL contract — no body, only the run-id header when
// one is given. Nothing here may change for an old caller.
{
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return new Response(JSON.stringify({ stopped: true }));
  };
  const ok = await stopChat('s1', 'run-1');
  assert.equal(ok, true);
  assert.equal(captured.url, '/api/chat/stop/s1');
  assert.equal(captured.options.body, undefined, 'an unscoped stop must send no body, exactly as before');
  assert.equal(captured.options.headers['X-Odysseus-Run-Id'], 'run-1');
}

// 2) scope="generation": pauses rather than cancelling; reads `paused` back.
{
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return new Response(JSON.stringify({ scope: 'generation', stopped: false, paused: true }));
  };
  const ok = await stopChat('s2', 'run-2', 'generation');
  assert.equal(ok, true, 'stopChat must read `paused` back when the server did not set `stopped`');
  assert.equal(captured.url, '/api/chat/stop/s2');
  assert.deepEqual(JSON.parse(captured.options.body), { scope: 'generation' });
  assert.equal(captured.options.headers['Content-Type'], 'application/json');
}

// 3) scope="task" / "work": sent as the JSON body's `scope`.
for (const scope of ['task', 'work']) {
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return new Response(JSON.stringify({ scope, stopped: true }));
  };
  const ok = await stopChat('s3', 'run-3', scope);
  assert.equal(ok, true);
  assert.deepEqual(JSON.parse(captured.options.body), { scope });
}

// 4) pauseChat posts to /api/chat/pause/{sid} with the run-id header, and
// reports what the server's `paused` field says.
{
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return new Response(JSON.stringify({ paused: true }));
  };
  const ok = await pauseChat('s4', 'run-4');
  assert.equal(ok, true);
  assert.equal(captured.url, '/api/chat/pause/s4');
  assert.equal(captured.options.method, 'POST');
  assert.equal(captured.options.headers['X-Odysseus-Run-Id'], 'run-4');
}
{
  globalThis.fetch = async () => new Response(JSON.stringify({ paused: false }));
  assert.equal(await pauseChat('s4b', 'run-4b'), false, 'pauseChat must report a false `paused` honestly');
}

// 5) steerChat defaults to mode "steer"; an explicit "queue" is sent as-is.
{
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return new Response(JSON.stringify({ ok: true, mode: 'steer' }));
  };
  const ok = await steerChat('s5', 'keep going but check the tests first');
  assert.equal(ok, true);
  assert.equal(captured.url, '/api/chat/steer/s5');
  assert.deepEqual(JSON.parse(captured.options.body), {
    text: 'keep going but check the tests first',
    mode: 'steer',
  });
}
{
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return new Response(JSON.stringify({ ok: true, mode: 'queue' }));
  };
  const ok = await steerChat('s6', 'send this when done', { mode: 'queue', runId: 'run-6' });
  assert.equal(ok, true);
  assert.deepEqual(JSON.parse(captured.options.body), { text: 'send this when done', mode: 'queue' });
  assert.equal(captured.options.headers['X-Odysseus-Run-Id'], 'run-6');
}

// 6) A 404 (no active run) is reported as false, not thrown.
{
  globalThis.fetch = async () => new Response(JSON.stringify({ error: 'no active run' }), { status: 404 });
  assert.equal(await steerChat('s7', 'hello'), false);
}

console.log('stop-menu adapters (stopChat scope / pauseChat / steerChat): all checks passed');
