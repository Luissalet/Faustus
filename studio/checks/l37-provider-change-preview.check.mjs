// SET-05 (lote 37): before the DEFAULT provider/model actually changes,
// `Settings.tsx`'s `providerChangeQuery`/`shouldConfirmBeforeSaving` decide
// whether the change is worth interrupting the save for at all — only an
// actual move from one endpoint to a DIFFERENT one, with a preview that
// both loaded and found a real difference, earns a confirmation dialog.
//
// Run by tests/test_l37_settings_provider_change_preview_js.py, or by hand:
//   node studio/checks/l37-provider-change-preview.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { resolve } from 'node:path';

// Settings.tsx's default export (`SettingsScreen`) pulls in the whole
// settings screen tree (Appearance, i18n, theme sync, dialogs, ...); a
// virtual entry that re-exports ONLY the two pure functions under test lets
// esbuild's tree-shaking drop most of that. What survives (`shell/theme.ts`,
// `shell/appearance.ts`) applies the saved theme to `document` the instant
// it is imported, unconditionally, by design (so the FIRST paint is already
// themed) — a real-browser assumption this check has no browser to satisfy.
// None of it is exercised by the assertions below (only
// `providerChangeQuery`/`shouldConfirmBeforeSaving` are), so a minimal
// `document`/`window`/`getComputedStyle` stand-in just lets that module-load
// side effect complete quietly, the same way `research-resume.check.mjs`
// stubs `globalThis.window` for a module it does not otherwise exercise.
const el = () => ({
  style: { setProperty() {}, removeProperty() {} },
  dataset: {},
  setAttribute() {}, removeAttribute() {}, toggleAttribute() {},
  appendChild() {}, remove() {},
  getContext() { return null; },
});
globalThis.window = globalThis;
globalThis.document = {
  documentElement: el(),
  head: el(),
  body: el(),
  createElement: el,
  querySelector: () => null,
  querySelectorAll: () => ({ forEach() {} }),
};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });

const settingsPath = resolve('studio/src/screens/Settings.tsx');
const result = await build({
  stdin: {
    contents: `export { providerChangeQuery, shouldConfirmBeforeSaving } from ${JSON.stringify(settingsPath)};`,
    resolveDir: process.cwd(),
    loader: 'ts',
  },
  bundle: true,
  format: 'esm',
  platform: 'node',
  write: false,
  treeShaking: true,
  loader: { '.css': 'empty' },
});
const { providerChangeQuery, shouldConfirmBeforeSaving } = await import(
  `data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`
);

// ── providerChangeQuery: only a REAL move to a DIFFERENT endpoint qualifies ──

assert.equal(
  providerChangeQuery('ep-a', {}), null,
  'nothing changed at all: no query',
);
assert.equal(
  providerChangeQuery('ep-a', { default_model: 'llama-3' }), null,
  'a model swap on the SAME endpoint changes neither privacy nor cost: no query',
);
assert.equal(
  providerChangeQuery('ep-a', { default_endpoint_id: 'ep-a' }), null,
  'default_endpoint_id "changed" to the value it already had: no query',
);
assert.equal(
  providerChangeQuery('ep-a', { default_endpoint_id: '' }), null,
  'clearing the default endpoint has no "to" to preview: no query',
);
assert.deepEqual(
  providerChangeQuery('ep-a', { default_endpoint_id: 'ep-b' }),
  { from: 'ep-a', to: 'ep-b' },
  'an actual move from ep-a to ep-b is exactly what the route needs to compare',
);
assert.deepEqual(
  providerChangeQuery(undefined, { default_endpoint_id: 'ep-b' }),
  { from: '', to: 'ep-b' },
  'no previous default at all still asks (from="") — the route itself reports ok:false for that side',
);

// ── shouldConfirmBeforeSaving: only a loaded preview with a REAL difference interrupts the save ──

assert.equal(
  shouldConfirmBeforeSaving({ ok: true, changes: ['privacy: local — ... → cloud — ...'] }),
  true,
  'a loaded preview that found a real difference must be confirmed',
);
assert.equal(
  shouldConfirmBeforeSaving({ ok: true, changes: [] }),
  false,
  'a loaded preview with no differences (e.g. same endpoint_kind) never interrupts the save',
);
assert.equal(
  shouldConfirmBeforeSaving({ ok: false, changes: [] }),
  false,
  'a preview that could not resolve one side (ok:false) has nothing meaningful to confirm',
);

console.log('ALL OK');
