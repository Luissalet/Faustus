// CMP-05 (deepens ADP-11): pure-logic checks for the NEW exports in
// adapters/activity.ts (`groupByProject`, `stableAttentionOrder`,
// `nextRunParam`, the extended `mergeAttention`) and adapters/attention.ts
// (`loadAttention`'s row parsing with the new lifecycle/waitCause/
// connectionHealth/signal/nextAction/projectId fields). Same esbuild+node
// pattern as activity-feed.check.mjs — no DOM, no network.
import assert from 'node:assert/strict';
import { build } from 'esbuild';

async function bundle(entry) {
  const result = await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', write: false });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

const { mergeAttention, groupByProject, stableAttentionOrder, nextRunParam, runProjectId, workflowFrom } =
  await bundle('studio/src/adapters/activity.ts');
const { loadAttention } = await bundle('studio/src/adapters/attention.ts');

// --- mergeAttention: new fields attach, project id flows through ----------
{
  const runs = [
    { id: 's1', kind: 'chat', title: 'A', status: 'running', repeats: 1, chat: { sessionId: 's1', runId: 'r1', model: 'm' } },
    { id: 's2', kind: 'chat', title: 'B', status: 'running', repeats: 1, chat: { sessionId: 's2', runId: 'r2', model: 'm' } },
  ];
  const rows = [{
    sessionId: 's1', kind: 'disconnected', reason: 'No recent events — is it disconnected?', priority: 3, since: 100, detail: '2 min', label: '', unread: true,
    lifecycle: 'running', waitCause: 'none', connectionHealth: 'disconnected', signal: { source: 'events', ageS: 130, lastEventAt: 100 },
    nextAction: 'reconnect', projectId: 'proj-1',
  }];
  const merged = mergeAttention(runs, rows);
  const a = merged.find((r) => r.id === 's1');
  assert.equal(a.attention.lifecycle, 'running');
  assert.equal(a.attention.connectionHealth, 'disconnected');
  assert.equal(a.attention.nextAction, 'reconnect');
  assert.equal(a.attention.signal.ageS, 130);
  assert.equal(a.projectId, 'proj-1', 'project id flows from the matched attention row onto the run');
  const b = merged.find((r) => r.id === 's2');
  assert.equal(b.attention, undefined, 'a session with no attention row stays untouched');
  assert.equal(runProjectId(a), 'proj-1');
  assert.equal(runProjectId(b), null);
}

// --- groupByProject: buckets, most-urgent project first, "no project" last
{
  const mk = (id, priority, projectId) => ({
    id, kind: 'chat', title: id, status: 'running', repeats: 1,
    attention: priority === null ? undefined : { priority },
    projectId,
  });
  const runs = [mk('a', 5, 'proj-b'), mk('b', 0, 'proj-a'), mk('c', null, null), mk('d', 3, 'proj-a')];
  const groups = groupByProject(runs);
  assert.deepEqual(groups.map((g) => g.projectId), ['proj-a', 'proj-b', null], 'most urgent project first, ungrouped last');
  assert.deepEqual(groups.find((g) => g.projectId === 'proj-a').runs.map((r) => r.id), ['b', 'd'], 'order within a group is preserved, not re-sorted');
}

// --- stableAttentionOrder: frozen keeps position, appends new at the end --
{
  const mk = (id) => ({ id, kind: 'chat', title: id, status: 'running', repeats: 1 });
  const fresh = [mk('a'), mk('b'), mk('c')];
  // Not frozen: passthrough, exactly the fresh order.
  assert.deepEqual(stableAttentionOrder(fresh, [], false).map((r) => r.id), ['a', 'b', 'c']);
  // Frozen with a previous order that reordered b before a, and a NEW row 'd'
  // that was not part of the previous render: previous order wins for a/b/c,
  // 'd' is appended at the end rather than reshuffling the frozen three.
  const reordered = [mk('b'), mk('d'), mk('a'), mk('c')];
  const frozen = stableAttentionOrder(reordered, ['chat-b', 'chat-a', 'chat-c'], true);
  assert.deepEqual(frozen.map((r) => r.id), ['b', 'a', 'c', 'd'], 'frozen order held, new row appended after it');
  // A row the previous order had is now gone: silently dropped, not a crash.
  const shrunk = stableAttentionOrder([mk('a'), mk('c')], ['chat-b', 'chat-a', 'chat-c'], true);
  assert.deepEqual(shrunk.map((r) => r.id), ['a', 'c']);
}

// --- nextRunParam: a stale action never overrides a selection the person
//     has already navigated to since it started -------------------------
{
  // The person is still on the run the action was about: it closes.
  assert.equal(nextRunParam('chat-s1', 'chat-s1', true), null);
  // The person has since opened a DIFFERENT run: the stale close is dropped —
  // this is the decisive test's "a late response does not change the
  // selected destination".
  assert.equal(nextRunParam('chat-s2', 'chat-s1', true), 'chat-s2');
  // Nothing open at all: still a no-op, not an error.
  assert.equal(nextRunParam(null, 'chat-s1', true), null);
}

// --- workflowFrom: a workflow run's own project id surfaces at top level --
{
  const wf = workflowFrom({ id: 'wf1', title: 'Report', status: 'running', project_id: 'proj-z', nodes: [] });
  assert.equal(wf.projectId, 'proj-z');
  assert.equal(runProjectId(wf), 'proj-z');
  const noProject = workflowFrom({ id: 'wf2', title: 'Report', status: 'running', nodes: [] });
  assert.equal(noProject.projectId, null);
}

// --- attention.ts: rowFrom parses the new fields, falls back safely -------
{
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    runs: [{
      session_id: 's1', kind: 'queued_model', reason: 'Queued for its turn', priority: 4, since: 5, detail: '#2', label: 'Chat', unread: true,
      lifecycle: 'running', wait_cause: 'gpu_queue', connection_health: 'live',
      signal: { source: 'events', age_s: 3, last_event_at: 5 }, next_action: 'open', project_id: 'proj-9',
    }, {
      // Deliberately missing every CMP-05 field — an OLDER server response
      // (or a stub in a test) must still parse, with honest fallbacks.
      session_id: 's2', kind: 'approval', reason: 'Waiting for your approval', priority: 0, since: 1, detail: '', label: '', unread: true,
    }],
    unread_count: 2,
  }), { status: 200 });
  try {
    const feed = await loadAttention();
    assert.equal(feed.rows.length, 2);
    const [r1, r2] = feed.rows;
    assert.equal(r1.lifecycle, 'running');
    assert.equal(r1.waitCause, 'gpu_queue');
    assert.equal(r1.connectionHealth, 'live');
    assert.equal(r1.signal.ageS, 3);
    assert.equal(r1.signal.source, 'events');
    assert.equal(r1.nextAction, 'open');
    assert.equal(r1.projectId, 'proj-9');
    // Fallbacks for the field-less row: never crash, never invent a truthy value.
    assert.equal(r2.lifecycle, 'waiting');
    assert.equal(r2.waitCause, 'none');
    assert.equal(r2.connectionHealth, 'live');
    assert.equal(r2.nextAction, 'open');
    assert.equal(r2.projectId, null);
    assert.equal(r2.signal.source, 'events');
    assert.equal(r2.signal.ageS, null);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

console.log('ok attention-cmp05: mergeAttention fields, groupByProject, stableAttentionOrder, nextRunParam, workflow project id, attention row parsing');
