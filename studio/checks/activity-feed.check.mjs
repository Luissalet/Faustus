import assert from 'node:assert/strict';
import { build } from 'esbuild';

const result = await build({ entryPoints: ['studio/src/adapters/activity.ts'], bundle: true, format: 'esm', platform: 'node', write: false });
const { conversationRuns, loadActivity, retainUnavailableRuns, workflowFrom, changeWorkflow, artifactLinks, renderFrom, cancelRender } = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
assert.deepEqual(artifactLinks([{id:'occ_safe', label:'Report.md', url:'https://evil.invalid'}, 'occ_safe', '../secret', null]),
  [{id:'occ_safe', label:'Report.md', url:'/api/artifacts/occ_safe/download'}]);
assert.equal(artifactLinks('not an array').length, 0);
const media = renderFrom({id:'m1',workflow:'image.product',status:'running',reason:'Downloading output',ended_at:'2026-09-07T12:00:00Z'}, 0);
assert.match(media.title, /image.product/);
assert.equal(media.detail, 'Downloading output');
assert.equal(media.finishedAt, '2026-09-07T12:00:00Z');
const progress = { runId: 'run-1', phase: 'tool', startedAt: 1000, elapsedS: 10, lastEventAt: 9000, tool: 'read_file', detail: 'notes.md' };
const snapshot = { running: ['a', 'b', 'a'], awaiting: ['a'], queued: { b: 2 }, runs: { a: 'run-1', b: 'run-2' }, details: { a: progress, b: progress } };
const rows = conversationRuns(snapshot, [{ id: 'a', name: 'My document', model: 'local-model' }]);
assert.equal(rows.length, 2, 'sessions are deduplicated');
assert.equal(rows[0].status, 'waiting', 'approval outranks tool phase');
assert.equal(rows[0].chat.runId, 'run-1');
assert.equal(rows[1].status, 'queued');
assert.match(rows[1].detail, /#2/);
assert.equal(rows[1].title, 'Conversation', 'missing title never hides a live run');
const working = conversationRuns({ ...snapshot, awaiting: [], queued: {} }, []);
assert.match(working[0].detail, /Using read file.*notes.md/);

const originalFetch = globalThis.fetch;
const fetched = [];
let failAll = false;
let failNames = false;
let failChat = false;
let live = true;
globalThis.fetch = async (path) => {
  fetched.push(path);
  if (failAll || (failNames && path === '/api/sessions') || (failChat && path === '/api/chat/activity')) throw new Error('offline');
  const body = path === '/api/chat/activity'
    ? { running: live ? ['a'] : [], runs: { a: 'run-1' }, details: {}, awaiting_approval: [], queued: {} }
    : path === '/api/sessions' ? [{ id: 'a', name: 'Local work' }]
    : path.includes('tasks/runs') ? { runs: [{ id: 'old', status: 'completed', task_name: 'Finished' }] }
    : path.includes('workflows/runs') ? { ok: true, runs: [] }
    : {};
  return new Response(JSON.stringify(body), { status: 200 });
};
try {
  const feed = await loadActivity();
  assert.equal(feed.runs[0].kind, 'chat', 'live work appears before completed work');
  assert.equal(feed.runs[0].title, 'Local work');
  failChat = true;
  const partial = await loadActivity();
  assert.deepEqual(partial.unavailableKinds, ['chat']);
  const retained = retainUnavailableRuns(feed.runs, partial);
  assert.equal(retained.find((r) => r.kind === 'chat').chat.runId, 'run-1');
  assert.equal(retained.find((r) => r.kind === 'chat').stale, true);
  assert.equal(retained.filter((r) => r.kind === 'task').length, 1, 'successful sources replace, not duplicate');
  assert.equal(retainUnavailableRuns(retained, partial).length, retained.length, 'repeat outage does not duplicate stale rows');
  failChat = false;
  const recovered = retainUnavailableRuns(retained, await loadActivity());
  assert.equal(recovered.find((r) => r.kind === 'chat').stale, undefined);
  failNames = true;
  const degraded = await loadActivity();
  assert.equal(degraded.runs[0].kind, 'chat');
  assert.deepEqual(degraded.degraded, ['conversation names']);
  assert.deepEqual(degraded.unavailableKinds, [], 'names failure does not make run state stale');
  live = false;
  fetched.length = 0;
  assert.ok(!retainUnavailableRuns(recovered, await loadActivity()).some((r) => r.kind === 'chat'), 'confirmed empty source removes ended conversations');
  assert.ok(!fetched.includes('/api/sessions'), 'idle refresh does not enumerate sessions');
  failAll = true;
  await assert.rejects(loadActivity(), /Could not read the activity/);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(loadActivity(controller.signal), { name: 'AbortError' });
  const workflow = workflowFrom({ id: 'wf', title: 'Report', status: 'paused', nodes: [{ id:'gate', status:'paused', approval_id:'human', needs:['draft'] }] });
  assert.equal(workflow.kind, 'workflow');
  assert.equal(workflow.status, 'waiting');
  assert.equal(workflow.workflow.nodes[0].approvalId, 'human');
  assert.deepEqual(workflow.workflow.nodes[0].needs, ['draft']);
  assert.equal(workflowFrom({id:'timer', status:'paused', nodes:[{id:'wait', status:'paused', wake_at:'2099-01-01T00:00:00Z'}]}).status, 'paused');
  assert.throws(() => workflowFrom({}), /Invalid workflow/);
  let actionPath;
  globalThis.fetch = async (path) => { actionPath = path; return new Response('{"ok":true}'); };
  await changeWorkflow('run with space', 'advance', 'approval node');
  assert.equal(actionPath, '/api/workflows/runs/run%20with%20space/resume/approval%20node');
  globalThis.fetch = async () => new Response('{"ok":false,"reason":"lost race"}');
  await assert.rejects(changeWorkflow('r', 'cancel'), /lost race/);
  await assert.rejects(cancelRender('r'), /lost race/);
} finally {
  globalThis.fetch = originalFetch;
}
console.log('ALL OK: live conversations, approval precedence, scoped run ids, partial failures, cancellation and offline handling');
