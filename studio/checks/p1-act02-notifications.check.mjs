// ACT-02: which runs the tray reports, and the dedupe key that lets a
// reconnect's fresh poll see the same pending approval without re-alerting
// (studio/src/shell/notifications.ts). The server dedupes too
// (tests/test_p1_act_02_notifications.py); this checks the client never
// even asks twice for the same run inside one poll-to-poll diff.
//
// Run by tests/test_p1_act_02_js.py, or by hand:
//   node studio/checks/p1-act02-notifications.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-notif-')), 'notifications.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'shell', 'notifications.ts')],
  bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent',
});
const { dedupeKeyForRun, payloadForRun, emitForNewRuns } = await import(pathToFileURL(out).href);

const waitingApproval = { id: 'a1', kind: 'approval', status: 'waiting', title: 'Approve publish', repeats: 1 };
const runningTask = { id: 't1', kind: 'task', status: 'running', title: 'Summarize', repeats: 1 };
const failedTask = { id: 't2', kind: 'task', status: 'failed', title: 'Import', error: 'boom', repeats: 1 };

assert.equal(dedupeKeyForRun(waitingApproval), 'approval:a1');
assert.deepEqual(payloadForRun(runningTask), null); // a running task is not yet worth an alert
assert.equal(payloadForRun(waitingApproval).type, 'approval');
assert.equal(payloadForRun(failedTask).type, 'task_failed');
assert.equal(payloadForRun(failedTask).detail, 'boom');

// The reconnect-storm behaviour, client side: the same run seen on twenty
// consecutive poll ticks must only ever be handed to emit() once.
let calls = 0;
globalThis.fetch = async () => {
  calls++;
  return new Response(JSON.stringify({ stored: true, notification: { id: 'n1' } }), { status: 200, headers: { 'Content-Type': 'application/json' } });
};
const seen = new Set();
for (let i = 0; i < 20; i++) {
  await emitForNewRuns([waitingApproval], seen);
}
assert.equal(calls, 1, `expected exactly one emit call across 20 identical poll ticks, got ${calls}`);

console.log('p1-act02-notifications.check.mjs OK');
