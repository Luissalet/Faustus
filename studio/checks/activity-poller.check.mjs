import assert from 'node:assert/strict';
import { build } from 'esbuild';
const built = await build({entryPoints:['studio/src/lib/activity-poller.ts'], bundle:true, format:'esm', write:false});
const {createActivityPoller} = await import(`data:text/javascript;base64,${Buffer.from(built.outputFiles[0].text).toString('base64')}`);
const calls = [], data = [], errors = [], states = [], timers = new Map();
let visible = true, serial = 0;
const poller = createActivityPoller({
  load: signal => new Promise((resolve, reject) => calls.push({signal, resolve, reject})),
  live: value => value.live, visible: () => visible,
  data: value => data.push(value), error: () => errors.push(true), refreshing: value => states.push(value),
  schedule: (fn, delay) => { timers.set(++serial, {fn, delay}); return serial; }, clear: id => timers.delete(id),
});
const settle = async () => { await Promise.resolve(); await Promise.resolve(); };
poller.start();
const second = poller.refresh();
assert.equal(calls[0].signal.aborted, true, 'superseded read is aborted');
calls[1].resolve({live:true, version:2}); await second;
calls[0].resolve({live:false, version:1}); await settle();
assert.deepEqual(data.map(x=>x.version), [2], 'late old response cannot overwrite fresh data');
assert.equal(timers.size, 1, 'one polling chain survives supersession');
assert.equal([...timers.values()][0].delay, 5000);
visible = false; poller.visibilityChanged();
assert.equal(timers.size, 0, 'hidden page stops polling');
visible = true; poller.visibilityChanged();
assert.equal(calls.length, 3, 'visible page refreshes immediately');
const oldStates = states.length;
poller.dispose(); calls[2].reject(new Error('late network failure')); await settle();
assert.equal(calls[2].signal.aborted, true);
assert.equal(timers.size, 0);
assert.equal(errors.length, 0, 'unmounted error does not update UI');
assert.equal(states.length, oldStates, 'unmounted finalizer does not update UI');
poller.start(); calls[3].resolve({live:false, version:3}); await settle();
assert.equal([...timers.values()][0].delay, 30000, 'StrictMode restart supports idle cadence');
const stalled = poller.refresh();
const deadline = [...timers.values()].find(x => x.delay === 15000);
deadline.fn();
assert.equal(calls[4].signal.aborted, true);
calls[4].resolve({live:true, version:4}); await stalled;
assert.equal(errors.length, 1, 'timed out response is never treated as successful');
assert.deepEqual(data.map(x=>x.version), [2,3]);
poller.dispose();
console.log('ALL OK: supersession, visibility, unmount, restart, timeout and single poll chain');
