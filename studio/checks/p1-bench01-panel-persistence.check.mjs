// P1 BENCH-01 - "Espacio de trabajo persistente": the panel state (open doc,
// open file, unsaved drafts) has to survive across the tab actually closing,
// with a stable per-conversation id, and opening a file must never silently
// close an unsaved doc/diff nor drop its draft.
//
// studio/src/screens/studio/panel.ts (reducer) and panel-storage.ts
// (read/persist) already carry this logic; this check proves it end to end
// against the real, deployed modules - not a reimplementation.
//
// Bundled with esbuild on the fly; run by
// tests/test_p1_bench01_panel_persistence.py, or by hand:
//   node studio/checks/p1-bench01-panel-persistence.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-bench01-'));
async function load(rel, name) {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', rel)], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const p = await load(join('screens', 'studio', 'panel.ts'), 'panel.mjs');
const ps = await load(join('screens', 'studio', 'panel-storage.ts'), 'panel-storage.mjs');

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

// A fake Storage that behaves like the real one for getItem/setItem -
// standing in for either sessionStorage or localStorage, since readPanel /
// persistPanels are storage-agnostic (see StoragePort in panel-storage.ts).
function fakeStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), _map: m };
}

const doc = { streaming: false, id: 'd1', title: 'Notes', language: 'markdown', content: 'hello', version: 1, suggestions: [] };

// ── Opening a file must not close an unsaved doc, nor drop its draft ──
{
  let s = p.panelReducer(p.initialPanel, { type: 'doc', doc });
  s = p.panelReducer(s, { type: 'draft', key: p.docKey(doc), draft: { text: 'unsaved edit', base: 'hello' } });
  const beforeDoc = s.doc, beforeDraft = s.drafts[p.docKey(doc)];
  const afterOpeningFile = p.panelReducer(s, { type: 'file', workspace: 'w', path: 'a.py' });
  assert(afterOpeningFile.doc === beforeDoc, 'opening a file keeps the open doc untouched');
  assert(afterOpeningFile.drafts[p.docKey(doc)] === beforeDraft, 'opening a file keeps the doc draft untouched');
  assert(afterOpeningFile.file?.path === 'a.py', 'the file itself does open');
}

// ── An entry with an unsaved draft is never evicted by `forget` ──
{
  let s = p.panelReducer(p.initialPanel, { type: 'doc', doc });
  s = p.panelReducer(s, { type: 'draft', key: p.docKey(doc), draft: { text: 'unsaved edit', base: 'hello' } });
  const forgotten = p.panelReducer(s, { type: 'forget', key: p.docKey(doc) });
  assert(forgotten === s, 'forget is a no-op while the key still has an unsaved draft');
  const cleared = p.panelReducer(s, { type: 'draft', key: p.docKey(doc), draft: null });
  const nowForgotten = p.panelReducer(cleared, { type: 'forget', key: p.docKey(doc) });
  assert(nowForgotten.documents.every((d) => d.id !== 'd1'), 'once the draft is gone, forget can evict it');
}

// ── Round-trip through storage: drafts and the open doc/file survive ──
{
  const store = fakeStorage();
  let s = p.panelReducer(p.initialPanel, { type: 'doc', doc });
  s = p.panelReducer(s, { type: 'draft', key: p.docKey(doc), draft: { text: 'unsaved edit', base: 'hello' } });
  s = p.panelReducer(s, { type: 'file', workspace: 'w', path: 'a.py' });
  ps.persistPanels(store, { conv1: s }, new Map());
  const restored = ps.readPanel(store, 'conv1');
  assert(restored.drafts[p.docKey(doc)]?.text === 'unsaved edit', 'the draft text survives a storage round-trip');
  assert(restored.documents.some((d) => d.id === 'd1'), 'the document itself is still known after restoring');
  assert(restored.files.some((f) => f.path === 'a.py'), 'the file is still known after restoring');
  assert(restored.streamDoc === null && restored.frames.length === 0 && restored.active === -1 && restored.live === false,
    'transient/streaming state is never restored from storage');
}

// ── An incognito key is never written to or read from storage ──
{
  const store = fakeStorage();
  let s = p.panelReducer(p.initialPanel, { type: 'doc', doc });
  ps.persistPanels(store, { 'private:conv1': s }, new Map());
  assert(store._map.size === 0, 'an incognito conversation writes nothing to storage');
  const restored = ps.readPanel(store, 'private:conv1');
  assert(restored.doc === null, 'reading an incognito key never returns a persisted doc');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
