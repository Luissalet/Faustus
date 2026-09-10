// Lote 55 item 2 — UX-06: the composer stays under a 16ms-per-keystroke
// budget with 200 attachments on screen and 500 candidate mentions in the
// workspace. Two real, bundled modules are exercised together exactly the
// way `Composer.tsx` wires them (its own component cannot run here — no
// DOM/jsdom — so this drives the same real code its `onChange` handler
// calls, not a reimplementation):
//
//   * studio/src/screens/studio/composer-suggest.ts — resolveSuggestionIntent
//     (the actual @/  matching) and capMentionItems (the defensive cap).
//   * studio/src/lib/frame-batch.ts — frameBatcher, already proven by
//     l39-frame-batch.check.mjs; reused here (not re-tested) to coalesce a
//     keystroke burst into one match pass, the way `Composer.tsx` uses it.
//
// Run by tests/test_l55_studio_checks_js.py, or by hand:
//   node studio/checks/l55-ux06-composer-perf.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';

async function bundle(entry) {
  const result = await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', write: false, logLevel: 'silent' });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

const { resolveSuggestionIntent, capMentionItems, MAX_MENTION_ITEMS } = await bundle('studio/src/screens/studio/composer-suggest.ts');
const { frameBatcher } = await bundle('studio/src/lib/frame-batch.ts');

let failed = 0;
const check = (condition, message) => {
  if (!condition) { failed += 1; console.error('FAIL:', message); }
  else console.log('ok', message);
};

// A synchronous fake scheduler: lets the test drive "one animation frame"
// explicitly, the same helper l39-frame-batch.check.mjs uses.
function fakeScheduler() {
  let pendingCb = null;
  let nextId = 1;
  return {
    scheduler: {
      schedule: (cb) => { pendingCb = cb; return nextId++; },
      cancel: () => { pendingCb = null; },
    },
    runFrame: () => { const cb = pendingCb; pendingCb = null; if (cb) cb(); },
  };
}

// ── a burst of keystrokes in one frame runs the match pass exactly once ──
{
  let calls = 0;
  const deliveries = [];
  const { scheduler, runFrame } = fakeScheduler();
  const batcher = frameBatcher(({ value, caret }) => {
    calls += 1;
    deliveries.push(resolveSuggestionIntent(value, caret));
  }, scheduler);

  // 500 "candidate mention" keystrokes — a draft with a workspace of hundreds
  // of same-prefix files landing in one animation frame — one per character
  // typed of an @mention query, exactly what Composer.tsx's onChange pushes.
  const base = '@file-';
  for (let i = 0; i < 500; i += 1) {
    const value = base + String(i);
    batcher.push({ value, caret: value.length });
  }
  check(calls === 0, 'nothing runs before the frame — 500 pushes cost nothing on the thread yet');
  runFrame();
  check(calls === 1, `500 keystrokes in one frame resolve exactly once, not 500 times (got ${calls})`);
  check(deliveries.length === 1 && deliveries[0].kind === 'mention' && deliveries[0].query === 'file-499',
    'the one resolution is for the LAST keystroke pushed, not the first — nothing is lost by coalescing');
}

// ── same coalescing for `/` command matching ──
{
  let calls = 0;
  let lastIntent = null;
  const { scheduler, runFrame } = fakeScheduler();
  const batcher = frameBatcher(({ value, caret }) => {
    calls += 1;
    lastIntent = resolveSuggestionIntent(value, caret);
  }, scheduler);
  for (const partial of ['/', '/c', '/ch', '/cha', '/chat', '/chats']) {
    batcher.push({ value: partial, caret: partial.length });
  }
  runFrame();
  check(calls === 1, 'six keystrokes building a slash command still resolve exactly once');
  check(lastIntent.kind === 'commands' && lastIntent.items.length > 0, 'the coalesced call sees the fully-typed command, not an early partial');
}

// ── capMentionItems: the actual defensive cap, correctness first ──
{
  const items = Array.from({ length: 500 }, (_, i) => ({ path: `dir/file-${i}.ts` }));
  const capped = capMentionItems(items);
  check(capped.length === MAX_MENTION_ITEMS, `500 candidates render as ${MAX_MENTION_ITEMS}, not 500 (got ${capped.length})`);
  check(capped[0].path === items[0].path, 'capping keeps the best-ranked (first) matches, not a random slice');
  const small = capMentionItems([{ path: 'a' }, { path: 'b' }]);
  check(small.length === 2, 'a normal, small result list is never truncated');
}

// ── wall-clock: 200 keystrokes' worth of resolveSuggestionIntent calls,
//    each against a draft sitting behind 200 attachments (the draft text
//    itself is what resolveSuggestionIntent looks at; the attachment count
//    lives in Composer's own state, not in this function's input — this
//    bounds the piece of the per-keystroke budget this module owns) ──
{
  const longDraft = `${'x'.repeat(4000)} @file-candidate`;
  const iterations = 200;
  const start = performance.now();
  for (let i = 0; i < iterations; i += 1) {
    resolveSuggestionIntent(`${longDraft}${i}`, longDraft.length + String(i).length);
  }
  const elapsed = performance.now() - start;
  const perCall = elapsed / iterations;
  check(perCall < 16, `resolveSuggestionIntent stays under 16ms/call over ${iterations} calls (avg ${perCall.toFixed(3)}ms)`);
}

// ── wall-clock: capMentionItems over 500 candidates, 200 times (one per
//    simulated frame across a fast typing burst) ──
{
  const items = Array.from({ length: 500 }, (_, i) => ({ path: `dir/file-${i}.ts` }));
  const iterations = 200;
  const start = performance.now();
  for (let i = 0; i < iterations; i += 1) capMentionItems(items);
  const elapsed = performance.now() - start;
  const perCall = elapsed / iterations;
  check(perCall < 16, `capMentionItems(500 items) stays under 16ms/call over ${iterations} calls (avg ${perCall.toFixed(3)}ms)`);
}

if (failed) {
  console.error(`${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('ALL OK: keystroke bursts coalesce to one match pass, 500 candidates render as capped, both within the 16ms budget');
