// Generation gate for Studio external-activity follow (#941).
// Run: node studio/checks/external-follow.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-ext-follow-')), 'externalFollow.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'screens', 'studio', 'externalFollow.ts')],
  bundle: true,
  format: 'esm',
  platform: 'node',
  outfile: out,
  logLevel: 'silent',
});
const { createExternalFollowGate, mayApplyExternalFollow } = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

{
  const gate = createExternalFollowGate();
  const a = gate.begin();
  const b = gate.begin();
  assert(gate.isCurrent(b) && !gate.isCurrent(a), 'later begin() invalidates the earlier token');
  gate.invalidate();
  assert(!gate.isCurrent(b), 'invalidate() drops the current token');
}

{
  // Deferred promises: idle→live then live→idle; terminal finishes first.
  const gate = createExternalFollowGate();
  const applied = [];
  const start = (label) => {
    const token = gate.begin();
    let resolve;
    const promise = new Promise((r) => {
      resolve = r;
    });
    promise.then((payload) => {
      if (
        !mayApplyExternalFollow({
          token,
          gate,
          visibleSession: 'sid-a',
          sid: 'sid-a',
          epoch: 1,
          currentEpoch: 1,
          hasController: false,
        })
      ) {
        applied.push(`${label}:skipped`);
        return;
      }
      applied.push(`${label}:${payload}`);
    });
    return { resolve };
  };
  const live = start('idle-live');
  const idle = start('live-idle');
  idle.resolve('terminal');
  live.resolve('stale');
  await Promise.resolve();
  await Promise.resolve();
  assert(
    applied.join(',') === 'live-idle:terminal,idle-live:skipped',
    `slow idle→live must not apply after live→idle (got ${applied.join(',')})`,
  );
}

{
  // Reverse order: idle→live completes before live→idle starts — both apply in order.
  const gate = createExternalFollowGate();
  const applied = [];
  const run = async (label, payload) => {
    const token = gate.begin();
    await Promise.resolve();
    if (
      mayApplyExternalFollow({
        token,
        gate,
        visibleSession: 'sid-a',
        sid: 'sid-a',
        epoch: 1,
        currentEpoch: 1,
        hasController: false,
      })
    ) {
      applied.push(`${label}:${payload}`);
    } else applied.push(`${label}:skipped`);
  };
  await run('idle-live', 'live-hist');
  await run('live-idle', 'terminal');
  assert(
    applied.join(',') === 'idle-live:live-hist,live-idle:terminal',
    `in-order transitions both apply (got ${applied.join(',')})`,
  );
}

{
  const gate = createExternalFollowGate();
  const token = gate.begin();
  assert(
    !mayApplyExternalFollow({
      token,
      gate,
      visibleSession: 'sid-b',
      sid: 'sid-a',
      epoch: 1,
      currentEpoch: 1,
      hasController: false,
    }),
    'SID change blocks apply',
  );
  assert(
    !mayApplyExternalFollow({
      token,
      gate,
      visibleSession: 'sid-a',
      sid: 'sid-a',
      epoch: 1,
      currentEpoch: 1,
      hasController: true,
    }),
    'local controller blocks apply (no double resume)',
  );
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
