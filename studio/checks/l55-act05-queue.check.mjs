// Lote 55 item 5 — ACT-05: studio/src/adapters/activity.ts's loadQueue /
// prioritizeQueueItem against routes/queue_routes.py's real wire shape
// (snake_case in, camelCase QueueItem out — same esbuild-bundle-and-stub-
// fetch shape studio/checks/activity-feed.check.mjs already uses for the
// rest of this adapter).
import assert from 'node:assert/strict';
import { build } from 'esbuild';

const result = await build({ entryPoints: ['studio/src/adapters/activity.ts'], bundle: true, format: 'esm', platform: 'node', write: false });
const { loadQueue, prioritizeQueueItem } = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);

const originalFetch = globalThis.fetch;
try {
  let seenPath;
  globalThis.fetch = async (path) => {
    seenPath = path;
    return new Response(JSON.stringify({
      ok: true,
      items: [
        { kind: 'agent_run', id: 'run-1', session_id: 's1', label: 'Chat 1', status: 'queued', position: 2, eta_seconds: null, started_at: 1000, elapsed_s: 5, reorderable: true },
        { kind: 'media_run', id: 'm1', session_id: 's1', label: 'sdxl.image', status: 'queued', position: null, eta_seconds: null, started_at: 2000, elapsed_s: 1, reorderable: false },
        // A row missing every optional field must still map cleanly to nulls/defaults —
        // routes/queue_routes.py only ever omits a field it could not measure, never sends `undefined`.
        { kind: 'bg_job', id: 'j1', status: 'running' },
      ],
      ts: 12345,
    }), { status: 200 });
  };
  const items = await loadQueue();
  assert.equal(seenPath, '/api/queue');
  assert.equal(items.length, 3);
  assert.deepEqual(items[0], {
    kind: 'agent_run', id: 'run-1', sessionId: 's1', label: 'Chat 1', status: 'queued',
    position: 2, etaSeconds: null, startedAt: 1000, elapsedS: 5, reorderable: true,
  });
  assert.equal(items[1].reorderable, false);
  assert.deepEqual(
    { sessionId: items[2].sessionId, label: items[2].label, position: items[2].position, reorderable: items[2].reorderable },
    { sessionId: '', label: 'j1', position: null, reorderable: false },
  );

  // The priority route: POSTs the right path, and a 409 ("no ordered queue
  // for this kind", routes/queue_routes.py) surfaces as a normal rejection
  // rather than a silently-ignored click.
  let posted;
  globalThis.fetch = async (path, init) => { posted = { path, method: init?.method }; return new Response('{"ok":true}', { status: 200 }); };
  await prioritizeQueueItem('agent_run', 'run 1');
  assert.equal(posted.path, '/api/queue/agent_run/run%201/priority');
  assert.equal(posted.method, 'POST');

  globalThis.fetch = async () => new Response(JSON.stringify({ detail: { reason: 'no_ordered_queue', message: 'bg_job has no client-orderable queue to reorder' } }), { status: 409 });
  await assert.rejects(prioritizeQueueItem('bg_job', 'j1'), /no client-orderable queue/);
} finally {
  globalThis.fetch = originalFetch;
}
console.log('ALL OK: queue feed mapping (incl. omitted-field defaults) and the priority route\'s success/refusal paths');
