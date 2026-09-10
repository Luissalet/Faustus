// ACT-06: a screen has (at least) four states — loading, empty, error (with
// a way out), and "neither of those": no permission, or the data on screen
// is from a version the server no longer has. Before this lote `EmptyState`
// had no vocabulary for the last two, so a screen reaching for them had to
// invent its own icon/copy/markup each time — which is exactly how a screen
// ends up covering three states and forgetting the fourth silently.
//
// This guard checks two different things on purpose:
//  1. `EmptyState` genuinely renders all four tones (a structural fact,
//     true regardless of which screen uses them).
//  2. Activity.tsx (ACT-01's screen, owned by this lote) actually reaches
//     loading/empty/error, and separately still gates every mutating action
//     behind its own "this is last-known, not current" banner — the
//     incompatible-version case, wired here as a disable-gate rather than a
//     full-screen EmptyState because the detail pane sits beside a live list,
//     not instead of it.
// It does NOT assert every one of Studio's 41 screens covers all four:
// most of those are owned by other lotes, and asserting a state nobody
// wired would just make this guard permanently red. See PENDIENTES/the
// batch report for the denied-state gap (no backend signal reaches Activity
// yet) rather than a fabricated usage here.
//
// Run by tests/test_studio_screen_states_js.py, or by hand:
//   node studio/checks/screen-states.check.mjs
import { readFileSync } from 'node:fs';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

// ── 1. EmptyState renders all four tones ──
const output = join(mkdtempSync(join(tmpdir(), 'faustus-states-')), 'fixture.mjs');
await build({
  stdin: {
    contents: `
    import React from 'react';
    import { renderToStaticMarkup } from 'react-dom/server';
    import { EmptyState } from './studio/src/components/EmptyState';
    export const render = (props) => renderToStaticMarkup(React.createElement(EmptyState, props));
  `, resolveDir: root, loader: 'tsx',
  },
  bundle: true, platform: 'node', format: 'esm', jsx: 'automatic',
  outfile: output, logLevel: 'silent',
  banner: { js: "import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" },
});
const { render } = await import(pathToFileURL(output).href);

const base = { title: 'Title', body: 'Body' };
{
  const html = render(base);
  assert(html.includes('data-tone="empty"'), 'no tone defaults to "empty" (backward compatible)');
  assert(!/role="alert"/.test(html), '"empty" does not interrupt with an alert role');
}
{
  const html = render({ ...base, tone: 'error', primaryAction: { label: 'Retry', onClick: () => {} } });
  assert(html.includes('data-tone="error"') && html.includes('role="alert"'), '"error" announces immediately and keeps its way out');
}
{
  const html = render({ ...base, tone: 'denied' });
  assert(html.includes('data-tone="denied"') && html.includes('role="alert"'), '"denied" is a distinct tone, not error/empty relabelled');
  assert(/<svg/.test(html), '"denied" has a default icon when the caller passes none (a lock, not a blank space)');
}
{
  const html = render({ ...base, tone: 'incompatible' });
  assert(html.includes('data-tone="incompatible"') && !/role="alert"/.test(html), '"incompatible" is its own tone and does not interrupt');
  assert(/<svg/.test(html), '"incompatible" has a default icon when the caller passes none');
}
{
  // A caller's own icon always wins over the tone default.
  const html = render({ ...base, tone: 'denied', icon: undefined });
  assert(html.includes('fs-empty__icon'), 'a tone with no caller icon still renders one');
}

// ── 2. Activity.tsx: loading / empty / error(+action) present, and the
//    incompatible-version gate is real, not just a comment ──
const activity = readFileSync(join(root, 'studio', 'src', 'screens', 'Activity.tsx'), 'utf-8');
assert(/<Skeleton\b/.test(activity), 'Activity has a loading state (Skeleton)');
assert(/<EmptyState\s+tone="error"/.test(activity), 'Activity has at least one tone="error" EmptyState (the full-screen "could not read" failure)');
assert(activity.includes("tone={degraded.length > 0 || failed ? 'error' : 'empty'}"), 'the in-list EmptyState switches between "empty" and "error" by what actually happened, not one fixed tone');
assert(activity.includes("primaryAction={{ label: t('Retry')"), 'the error state offers a way out, not just an apology');
const staleGateCount = (activity.match(/disabled=\{currentStale/g) || []).length;
assert(staleGateCount >= 8, `every mutating action in the detail pane is gated behind "stale" (found ${staleGateCount} gates)`);
assert(activity.includes("t('Last known activity. Refresh before taking action.')"), 'the incompatible/stale state explains itself, not just disables buttons silently');

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
