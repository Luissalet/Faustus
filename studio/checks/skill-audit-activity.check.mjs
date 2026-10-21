import assert from 'node:assert/strict';
import { build } from 'esbuild';

const bundled = await build({ entryPoints: ['studio/src/adapters/activity.ts'], bundle: true, format: 'esm', platform: 'node', write: false });
const adapter = await import(`data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString('base64')}`);
const originalFetch = globalThis.fetch;
let active = true;
let unavailable = false;
let replacement = false;
const calls = [];
const job = () => ({ id: 'skill-audit:own', kind: 'skill_audit', active, status: active ? 'running' : 'done', scope: 'scheduled', owner: null,
  model: 'global-worker', teacher: 'configured-teacher', phase: active ? 'review' : 'finished', phase_model: active ? 'global-worker' : null,
  attempt: 1, total: 8, done: active ? 2 : 8, current: active ? 'Unicode 東京 skill' : null, started: 1700000000, finished: active ? null : 1700000400, error: null });
globalThis.fetch = async (path, options = {}) => {
  calls.push({ path, method: options.method ?? 'GET' });
  if (path === '/api/chat/activity' && unavailable) throw new Error('offline');
  let body = path === '/api/chat/activity' ? { running: [], runs: {}, details: {}, queued: {}, awaiting_approval: [], background_jobs: [job()] }
    : path.includes('/api/skills/audit-all/status?') ? { ...job(), id: replacement ? 'skill-audit:other' : job().id, results: [{ skill: 'Unicode 東京 skill', result: 'error', error: 'real test error' }], log: ['review finished'] }
    : path.includes('workflows/runs') ? { ok: true, runs: [] } : {};
  return new Response(JSON.stringify(body), { status: 200 });
};
try {
  const live = await adapter.loadActivity();
  assert.equal(live.runs.length, 1, 'a scheduled job must survive the no-chat early return');
  const row = live.runs[0];
  assert.equal(row.kind, 'skill_audit');
  assert.equal(row.status, 'running');
  assert.equal(row.chat, undefined, 'audit identity is never a chat stop target');
  assert.equal(row.audit.done, 2);
  assert.equal(row.audit.current, 'Unicode 東京 skill');
  assert.equal(row.audit.phaseModel, 'global-worker');
  assert.equal(row.audit.teacher, 'configured-teacher');
  assert.ok(!calls.some(c => c.path === '/api/sessions'), 'no chat name lookup for a background-only response');
  assert.equal((await adapter.loadActivity()).runs[0].id, row.id, 'a quiet interval must keep the same job');
  unavailable = true;
  const partial = await adapter.loadActivity();
  assert.ok(partial.unavailableKinds.includes('skill_audit'));
  assert.equal(adapter.retainUnavailableRuns(live.runs, partial)[0].stale, true);
  unavailable = false;
  active = false;
  const terminal = await adapter.loadActivity();
  assert.equal(terminal.runs[0].status, 'succeeded');
  assert.equal(terminal.runs[0].audit.active, false);
  const detail = await adapter.skillAuditResult(row.id);
  assert.equal(detail.id, row.id);
  assert.equal(detail.results[0].error, 'real test error');
  assert.ok(calls.some(c => c.path === '/api/skills/audit-all/status?job_id=skill-audit%3Aown'));
  replacement = true;
  await assert.rejects(adapter.skillAuditResult(row.id), /audit|Audit/);
  assert.ok(calls.every(c => c.method === 'GET'), 'reading activity or its results must never dispatch work');
  const cancelling = adapter.skillAuditRuns({ backgroundJobs: [{ ...row.audit, active: true, status: 'cancelled' }] });
  assert.equal(cancelling[0].status, 'running', 'cancellation is not finished before the executor finally runs');
} finally {
  globalThis.fetch = originalFetch;
}
console.log('ALL OK: background-only work, quiet intervals, terminal results, exact identities and degraded reads');
