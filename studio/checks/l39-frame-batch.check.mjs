// PERF-01/UX-05 (Lote 39): `frameBatcher` (studio/src/lib/frame-batch.ts)
// coalesces rapid pushes into at most one delivery per scheduled frame —
// the mechanism `Transcript.tsx`'s `useFrameBatched` hook wraps to keep a
// streaming turn's card from repainting once per delta. Drives the real
// module (bundled with esbuild, not a re-implementation) with a synchronous
// fake scheduler, so "one delivery per frame" is provable without a browser.
//
// Run by tests/test_studio_l39_frame_batch_js.py, or by hand:
//   node studio/checks/l39-frame-batch.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-frame-batch-')), 'frame-batch.mjs');
await build({
  entryPoints: [join(root, 'studio/src/lib/frame-batch.ts')],
  bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent',
});
const { frameBatcher } = await import(pathToFileURL(out).href);

// A fake scheduler that queues callbacks instead of running them: lets the
// test drive "one frame" explicitly instead of racing a real rAF.
function fakeScheduler() {
  let pendingCb = null;
  let nextId = 1;
  return {
    scheduler: {
      schedule: (cb) => {
        pendingCb = cb;
        return nextId++;
      },
      cancel: () => {
        pendingCb = null;
      },
    },
    runFrame: () => {
      const cb = pendingCb;
      pendingCb = null;
      if (cb) cb();
    },
    hasPending: () => pendingCb !== null,
  };
}

let failed = 0;
const check = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else {
    console.log('ok', message);
  }
};

// ── many pushes inside one frame deliver exactly once, with the LAST value ──
{
  const delivered = [];
  const { scheduler, runFrame } = fakeScheduler();
  const batcher = frameBatcher((v) => delivered.push(v), scheduler);
  batcher.push('t');
  batcher.push('te');
  batcher.push('tex');
  batcher.push('text');
  check(delivered.length === 0, 'nothing delivered before the frame runs');
  runFrame();
  check(delivered.length === 1, 'exactly one delivery for four pushes in one frame');
  check(delivered[0] === 'text', 'the delivered value is the LAST one pushed, not the first');
}

// ── a second frame with no new pushes delivers nothing ──
{
  const delivered = [];
  const { scheduler, runFrame } = fakeScheduler();
  const batcher = frameBatcher((v) => delivered.push(v), scheduler);
  batcher.push(1);
  runFrame();
  runFrame();
  check(delivered.length === 1, 'a frame with nothing pushed since the last delivery delivers nothing');
}

// ── two separate frames each deliver their own value ──
{
  const delivered = [];
  const { scheduler, runFrame } = fakeScheduler();
  const batcher = frameBatcher((v) => delivered.push(v), scheduler);
  batcher.push('a');
  runFrame();
  batcher.push('b');
  runFrame();
  check(delivered.length === 2 && delivered[0] === 'a' && delivered[1] === 'b', 'two frames, two deliveries, in order');
}

// ── cancel drops whatever was pending, delivering nothing ──
{
  const delivered = [];
  const { scheduler, runFrame, hasPending } = fakeScheduler();
  const batcher = frameBatcher((v) => delivered.push(v), scheduler);
  batcher.push('never');
  batcher.cancel();
  check(!hasPending(), 'cancel actually cancels the scheduled frame');
  runFrame();
  check(delivered.length === 0, 'cancel delivers nothing, even once the frame would have run');
}

// ── flush delivers immediately, without waiting for the frame ──
{
  const delivered = [];
  const { scheduler, hasPending } = fakeScheduler();
  const batcher = frameBatcher((v) => delivered.push(v), scheduler);
  batcher.push('now');
  batcher.flush();
  check(delivered.length === 1 && delivered[0] === 'now', 'flush delivers the pending value right away');
  check(!hasPending(), 'flush cancels the frame it no longer needs');
}

// ── flush with nothing pending is a harmless no-op ──
{
  const delivered = [];
  const { scheduler } = fakeScheduler();
  const batcher = frameBatcher((v) => delivered.push(v), scheduler);
  batcher.flush();
  check(delivered.length === 0, 'flush with nothing pushed delivers nothing');
}

if (failed) {
  console.error(`${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('ALL OK');
