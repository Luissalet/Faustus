// CMP-01/02/03 (W2-A1): studio/src/lib/docSession.ts — the shared document
// session (identity/baseRevision/draft/selection/undo-redo/proposals) and
// the pure occurrence-lookup CMP-02 needs to stop `applySuggestion` from
// ever applying a repeated `find` at its first match.
//
// Bundled with esbuild on the fly, same pattern as
// studio/checks/panel.check.mjs; run by tests/test_cmp01_doc_session_js.py,
// or by hand:
//   node studio/checks/doc_session.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-docsession-'));

// docSession.ts imports `react` (useSyncExternalStore) only for the
// `useDocSession` hook — nothing this check exercises calls it, but esbuild
// still needs something to resolve the import to. A one-file stub avoids
// pulling in all of React (and its own peer requirements) just to bundle a
// module whose non-hook exports are plain functions.
const reactStub = join(dir, 'react-stub.mjs');
await (await import('node:fs/promises')).writeFile(reactStub, 'export function useSyncExternalStore(){throw new Error("not exercised by this check");}\n');

async function load(rel, name) {
  const out = join(dir, name);
  await build({
    entryPoints: [join(root, 'studio', 'src', rel)],
    bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent',
    alias: { react: reactStub },
  });
  return import(pathToFileURL(out).href);
}

// localStorage: try/catch-wrapped in the module under test, but a real
// Map-backed fake here lets the persistence tests actually assert on it.
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
// `sendComposerContext` dispatches a CustomEvent on `window` — Node has no
// `window`, but does have a spec `EventTarget`/`CustomEvent`.
globalThis.window = new EventTarget();

const ds = await load(join('lib', 'docSession.ts'), 'docSession.mjs');

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

// ── Identity, draft, dirty ──
{
  store.clear();
  const s0 = ds.getSession('d1', { content: 'hello world', version: 3 });
  assert(s0.baseRevision === 3 && s0.baseText === 'hello world', 'a fresh session seeds from the given server base');
  assert(!ds.isDirty(s0), 'no draft yet: not dirty');
  assert(ds.currentText(s0) === 'hello world', 'currentText reads the base with no draft');

  const s1 = ds.setDraftText('d1', 'hello there');
  assert(ds.isDirty(s1), 'typing makes it dirty');
  assert(ds.currentText(s1) === 'hello there', 'currentText reads the draft once there is one');
  assert(JSON.parse(localStorage.getItem('faustus.docSession.draft.d1')).text === 'hello there', 'a dirty draft is persisted to localStorage');

  ds.setDraftText('d1', 'hello world'); // typed back to exactly the base
  const s2 = ds.getSession('d1');
  assert(!ds.isDirty(s2), 'typing back to the base text is not dirty');
  assert(localStorage.getItem('faustus.docSession.draft.d1') === null, 'and the localStorage row is dropped once not dirty');
}

// ── A document nobody touched is never "dirty" across a mode switch ──
{
  store.clear();
  const s = ds.getSession('untouched', { content: 'same', version: 1 });
  assert(!ds.isDirty(s), 'opening a document creates no draft by itself');
  // Re-reading (simulating a different mounted surface — the panel vs. the
  // full editor) must see the exact same, still-clean session.
  const again = ds.getSession('untouched', { content: 'same', version: 1 });
  assert(again === s, 'the SAME session object is returned to a second surface (module singleton, not per-component state)');
  assert(!ds.isDirty(again), 'and it is still not dirty — nothing calls setDraftText just by being read');
}

// ── sync(): follows the server when clean, preserves a draft when dirty ──
{
  store.clear();
  const clean = ds.getSession('c1', { content: 'v1', version: 1 });
  const synced = ds.sync('c1', { content: 'v2 from the agent', version: 2 });
  assert(synced.baseText === 'v2 from the agent' && synced.baseRevision === 2, 'not dirty: the session just follows the server');
  assert(!synced.rebasedPending, 'no draft existed, so nothing needed preserving');

  ds.getSession('d2', { content: 'v1', version: 1 });
  ds.setDraftText('d2', 'my edit', { record: true });
  const rebased = ds.sync('d2', { content: 'agent moved it', version: 5 });
  assert(rebased.draftText === 'my edit', 'dirty: the draft survives a server update under it');
  assert(rebased.baseText === 'agent moved it' && rebased.baseRevision === 5, 'but the base moves forward, so a later save is against the new revision');
  assert(rebased.rebasedPending, 'flagged: the surface should tell the person their draft outlived a server change');
  assert(ds.isDirty(rebased), 'still dirty — the draft never silently became "the same as the base"');
}

// ── markSaved(): clears the draft, the flag, and the localStorage row ──
{
  store.clear();
  ds.getSession('d3', { content: 'base', version: 1 });
  ds.setDraftText('d3', 'edited', { record: true });
  ds.sync('d3', { content: 'rebased', version: 2 });
  const saved = ds.markSaved('d3', { content: 'edited', version: 3 });
  assert(!ds.isDirty(saved), 'a just-saved session is not dirty');
  assert(!saved.rebasedPending, 'and the rebase flag clears with it');
  assert(localStorage.getItem('faustus.docSession.draft.d3') === null, 'no draft left in storage after a save');
}

// ── A draft in localStorage hydrates a session not yet touched this page
//    load — the "closed the tab, reopened later / after a reload" path.
//    Written to storage directly (not through setDraftText) so this
//    exercises getSession's OWN fallback, on a doc id nothing above has
//    ever called getSession/setDraftText with. ──
{
  store.clear();
  localStorage.setItem('faustus.docSession.draft.neverseen', JSON.stringify({ text: 'stored draft', base: 'base text', baseRevision: 4 }));
  const s = ds.getSession('neverseen'); // no serverBase — nothing to prefer over the stored draft
  assert(s.draftText === 'stored draft' && s.baseText === 'base text' && s.baseRevision === 4, 'a session never touched this page load still picks up an unsaved draft left in localStorage');
}

// ── forgetSession(): drops the draft for good (closing a tab), not just a
//    mode switch — distinct from discardDraft, which only clears the draft ──
{
  store.clear();
  ds.getSession('fg1', { content: 'base', version: 1 });
  ds.setDraftText('fg1', 'unsaved edit', { record: true });
  assert(localStorage.getItem('faustus.docSession.draft.fg1') !== null, 'the draft is in storage before forgetting');
  ds.forgetSession('fg1');
  assert(localStorage.getItem('faustus.docSession.draft.fg1') === null, 'forgetSession drops the stored draft too — it is "close for good", not "switch away for now"');
}

// ── Undo / redo ──
{
  store.clear();
  ds.getSession('u1', { content: 'base', version: 1 });
  ds.setDraftText('u1', 'edit one', { record: true });
  ds.setDraftText('u1', 'edit two', { record: true });
  let s = ds.getSession('u1');
  assert(ds.currentText(s) === 'edit two', 'two recorded edits: the latest is current');
  s = ds.undo('u1');
  assert(ds.currentText(s) === 'edit one', 'undo goes back one recorded step');
  s = ds.undo('u1');
  assert(ds.currentText(s) === 'base', 'undo again reaches the base');
  s = ds.undo('u1');
  assert(ds.currentText(s) === 'base', 'undoing past the bottom is a no-op, not an error');
  s = ds.redo('u1');
  assert(ds.currentText(s) === 'edit one', 'redo replays what undo took back');
  s = ds.redo('u1');
  assert(ds.currentText(s) === 'edit two', 'redo again reaches the latest edit');
  s = ds.redo('u1');
  assert(ds.currentText(s) === 'edit two', 'redoing past the top is a no-op');

  // A NEW recorded edit after an undo drops the abandoned redo branch —
  // standard undo/redo semantics, never a resurrected "future" edit.
  ds.undo('u1');
  ds.setDraftText('u1', 'a different edit two', { record: true });
  const after = ds.getSession('u1');
  assert(after.redoStack.length === 0, 'a fresh recorded edit clears the redo stack');
}

// ── discardDraft(): back to the base, nothing left to undo to ──
{
  store.clear();
  ds.getSession('disc1', { content: 'base', version: 1 });
  ds.setDraftText('disc1', 'edit', { record: true });
  const s = ds.discardDraft('disc1');
  assert(!ds.isDirty(s), 'discarding clears dirty');
  assert(s.undoStack.length === 0 && s.redoStack.length === 0, 'and the history with it — there is no edit left to step back through');
}

// ── Occurrence lookup (CMP-02): never guess the first match ──
{
  const zero = ds.findOccurrences('no needle here', 'xyz');
  assert(zero.length === 0, 'zero occurrences is an empty list, not a guess');

  const one = ds.findOccurrences('a needle in a haystack', 'needle');
  assert(one.length === 1 && one[0].start === 2, 'exactly one occurrence is found at its real offset');

  const text = 'The cat sat. The cat ran. The dog sat.';
  const many = ds.findOccurrences(text, 'The cat');
  assert(many.length === 2, 'a repeated phrase reports EVERY occurrence, not just the first');
  assert(many[0].start === 0 && many[1].start === 13, 'each occurrence carries its own real offset');
  assert(many[0].after.startsWith(' sat.') && many[1].after.startsWith(' ran.'), 'and its own distinguishing context — this is what lets a person tell them apart');

  const overlap = ds.findOccurrences('aaaa', 'aa');
  assert(overlap.length === 3, 'overlapping occurrences are all reported, never silently collapsed');

  const empty = ds.findOccurrences('anything', '');
  assert(empty.length === 0, 'an empty needle matches nothing (never "matches everywhere")');
}

// ── replaceAt(): replaces exactly the given span, never a text-wide match ──
{
  const text = 'The cat sat. The cat ran.';
  const occ = ds.findOccurrences(text, 'The cat');
  const out = ds.replaceAt(text, occ[1].start, occ[1].end, 'The dog');
  assert(out === 'The cat sat. The dog ran.', 'only the chosen occurrence changes; the other survives untouched');
}

// ── mergeSuggestions(): additive, deduped by id ──
{
  store.clear();
  ds.getSession('sg1', { content: 'x', version: 1 });
  ds.mergeSuggestions('sg1', [{ id: 'a', find: 'x', replace: 'y', reason: '' }]);
  ds.mergeSuggestions('sg1', [{ id: 'a', find: 'x', replace: 'y', reason: '' }, { id: 'b', find: 'p', replace: 'q', reason: '' }]);
  const s = ds.getSession('sg1');
  assert(s.suggestions.length === 2, 'a re-sent id is not duplicated; a genuinely new one is appended');
  assert(s.suggestions.map((x) => x.id).join(',') === 'a,b', 'insertion order is preserved');
}

// ── Selection ranges ──
{
  store.clear();
  ds.getSession('sel1', { content: 'hello world', version: 1 });
  const s = ds.setSelection('sel1', [{ start: 0, end: 5 }]);
  assert(s.selection.length === 1 && s.selection[0].start === 0 && s.selection[0].end === 5, 'a selection is a range, not a copy of the text');
}

// ── Selection/comments -> composer: a structured event, never a fake user message ──
{
  let received = null;
  const onEvt = (e) => { received = e.detail; };
  window.addEventListener(ds.COMPOSER_CONTEXT_EVENT, onEvt);
  ds.sendComposerContext({ doc: { id: 'd9', title: 'Doc' }, ranges: [{ start: 0, end: 4 }], action: 'clarify', items: [{ quote: 'text' }] });
  window.removeEventListener(ds.COMPOSER_CONTEXT_EVENT, onEvt);
  assert(received && received.doc.id === 'd9' && received.action === 'clarify' && received.items[0].quote === 'text', 'the composer receives a typed {doc, ranges, action, items} reference, not a chat message written on the user\'s behalf');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
