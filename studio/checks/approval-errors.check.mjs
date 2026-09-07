import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-approval-errors-')), 'chat.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/chat.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
const { sendTurn } = await import(pathToFileURL(out).href);

for (const body of [
  { detail: { code: 'tool_approval_expired', message: 'The approval expired. Request a fresh card.' } },
  { detail: 'The model endpoint was removed.' },
  { error: 'SESSION_NOT_FOUND', message: 'This conversation no longer exists.' },
]) {
  globalThis.fetch = async (_url, init) => {
    assert.equal(init.body.get('message'), '');
    assert.equal(init.body.get('tool_approval_id'), 'approval-fixture');
    return new Response(JSON.stringify(body), { status: 400, headers: { 'Content-Type': 'application/json' } });
  };
  const events = [];
  for await (const event of sendTurn({ sessionId: 's1', message: '', mode: 'chat',
    approval: { id: 'approval-fixture', decision: 'approve' } })) events.push(event);
  const expected = typeof body.detail === 'string' ? body.detail : body.detail?.message || body.message;
  assert.equal(events[0].message, expected);
  assert.equal(events[0].type, 'error');
}
const modelOut = join(dirname(out), 'model.mjs');
await build({ entryPoints: [join(root, 'studio/src/screens/studio/model.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: modelOut, logLevel: 'silent' });
const model = await import(pathToFileURL(modelOut).href);
const parked = model.blankTurn('assistant', 'Allow this task to continue?');
parked.ask = { kind: 'tool_approval', question: parked.text, approvalId: 'a1', options: [], multi: false };
parked.live = model.newLive(1000);
parked.error = 'Previous network failure';
parked.steps = [{ id: 'step1', state: 'waiting', tool: 'project_objectives', label: 'Objectives', round: 1 }];
const resumed = model.beginApproval(parked, 500_000);
assert.equal(resumed.live.lastAt, 500_000);
assert.equal(resumed.live.phaseAt, 500_000);
assert.equal(resumed.ask, parked.ask);
assert.equal(resumed.error, undefined);
const failed = model.apply(resumed, { type: 'error', message: 'Try again' });
assert.equal(failed.ask, parked.ask, 'failed request retains the exact card');
const retired = model.closeApproval(parked, 'superseded');
assert.equal(retired.ask, undefined);
assert.equal(retired.approval.decision, 'superseded');
assert.equal(retired.steps[0].state, 'cancelled');
assert.equal(retired.text, '');
assert.equal(parked.ask.approvalId, 'a1', 'original state was not mutated');
const restored = model.restoreFromMetadata(parked, { tool_events: [{ tool: 'project_objectives',
  ask_user: { ...parked.ask, approval_id: 'a1', resolved: 'superseded' } }] });
assert.equal(restored.ask, undefined, 'metadata refresh does not resurrect an old approval');
assert.equal(restored.approval.decision, 'superseded');
assert.equal(restored.steps[0].state, 'cancelled');
assert.equal(restored.steps[0].meta, retired.steps[0].meta);
const restoredParked = model.restoreFromMetadata(parked, { tool_events: [{ tool: 'project_objectives',
  exit_code: null, output: 'Waiting for an exact user approval',
  ask_user: { ...parked.ask, approval_id: 'a1', resolved: 'superseded' } }] });
assert.equal(restoredParked.steps[0].state, 'cancelled');
assert.equal(restoredParked.steps[0].meta, retired.steps[0].meta);
console.log('ALL OK');
