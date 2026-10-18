// Lote 70a, punto A.14 (Studio half): `adapters/session-draft.ts` — the
// client for `GET/PUT /api/sessions/{sid}/draft` (`src/session_draft.py`,
// `routes/session_routes.py`) that `Studio.tsx` merges with its own
// `localStorage` draft ("most recent wins" on load, debounced 1s on write).
//
// Run by tests/test_l70_a14_studio_draft_sync_js.py, or by hand:
//   node studio/checks/l70-a14-session-draft-sync.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-a14-draft-')), 'session-draft.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/session-draft.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
const { loadSessionDraft, saveSessionDraft, emptySessionDraft } = await import(pathToFileURL(out).href);

let failed = 0;
const check = (cond, msg) => { if (!cond) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };
const originalFetch = globalThis.fetch;

// ── loadSessionDraft(): GET, decodes the wire's snake_case ──
{
  let seenPath;
  globalThis.fetch = async (path) => { seenPath = path; return new Response(JSON.stringify({ text: 'half a sentence', attachment_ids: ['att1'], updated_at: 1234.5 }), { status: 200 }); };
  const d = await loadSessionDraft('sess 1');
  check(seenPath === '/api/sessions/sess%201/draft', 'loadSessionDraft() encodes the session id and hits GET .../draft');
  check(d.text === 'half a sentence', 'loadSessionDraft() decodes text');
  check(Array.isArray(d.attachmentIds) && d.attachmentIds[0] === 'att1', 'loadSessionDraft() decodes attachment_ids as attachmentIds');
  check(d.updatedAt === 1234.5, 'loadSessionDraft() decodes updated_at as updatedAt (unix seconds, unconverted)');
}

// ── loadSessionDraft(): a 404 (unknown/foreign session) rejects, not a silent empty ──
{
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'Session not found' }), { status: 404 });
  let threw = false;
  try { await loadSessionDraft('nope'); } catch { threw = true; }
  check(threw, 'loadSessionDraft() rejects on a non-2xx so the caller can fall back to the local draft');
}

// ── saveSessionDraft(): PUT with the exact wire shape the route expects ──
{
  let seenPath, seenInit;
  globalThis.fetch = async (path, init) => { seenPath = path; seenInit = init; return new Response(JSON.stringify({ text: 'saved', attachment_ids: [], updated_at: 99 }), { status: 200 }); };
  const d = await saveSessionDraft('sess-2', 'saved', []);
  check(seenPath === '/api/sessions/sess-2/draft', 'saveSessionDraft() targets PUT .../draft');
  check(seenInit.method === 'PUT', 'saveSessionDraft() uses PUT');
  const body = JSON.parse(seenInit.body);
  check(body.text === 'saved' && Array.isArray(body.attachment_ids), 'saveSessionDraft() sends {text, attachment_ids}');
  check(d.text === 'saved', 'saveSessionDraft() decodes the response the same way loadSessionDraft() does');
}

check(emptySessionDraft.text === '' && emptySessionDraft.updatedAt === 0, 'emptySessionDraft is the zero record');

globalThis.fetch = originalFetch;

// Run the actual Studio draft effects with React. A direct saved-chat mount
// must read the server before its debounce can erase a draft on another device.
const { Window } = await import('happy-dom');
const window = new Window();
globalThis.window = window;
globalThis.document = window.document;
Object.defineProperty(globalThis, 'navigator', { value: window.navigator, configurable: true });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const React = await import('react');
const { createRoot } = await import('react-dom/client');
const source = process.env.QA869_STUDIO_BASELINE
  ? execFileSync('git', ['show', '966b9195:studio/src/screens/Studio.tsx'], { cwd: root, encoding: 'utf8' })
  : readFileSync(join(root, 'studio/src/screens/Studio.tsx'), 'utf8');
const effects = source.slice(source.indexOf('  const [draft, setDraft]'), source.indexOf('  const sendingMessage ='));
const harnessOut = join(dirname(out), 'draft-effects.mjs');
await build({ stdin: { contents: `
  import React, {useState,useEffect,useRef} from '${pathToFileURL(join(root, 'node_modules/react/index.js')).href}';
  export function Harness({sessionId,knobs,fixture}) {
    const {readDraftFor,readDraftUpdatedAtFor,writeDraftFor,consumeExcursoDraftSeed,
      loadSessionDraft,saveSessionDraft,readAttachmentsFor,writeAttachmentsFor,attachmentsFromMetadata,setTimeout,clearTimeout}=fixture;
    ${effects}
    fixture.screen={draft,attachments,setDraft,setAttachments};
    return null;
  }`, loader: 'tsx', resolveDir: root }, bundle: true, packages: 'external',
  external: [pathToFileURL(join(root, 'node_modules/react/index.js')).href],
  platform: 'node', format: 'esm', outfile: harnessOut, logLevel: 'silent' });
const { Harness } = await import(pathToFileURL(harnessOut).href);
function fixture() {
  const texts = new Map(), times = new Map(), files = new Map(), loads = [], saves = [], timers = new Map();
  let timerId = 0;
  return { texts, times, files, loads, saves, timers,
    readDraftFor: id => texts.get(id) ?? '', readDraftUpdatedAtFor: id => times.get(id) ?? 0,
    writeDraftFor: (id, text) => { texts.set(id, text); times.set(id, Date.now()); },
    consumeExcursoDraftSeed: () => '',
    attachmentsFromMetadata: meta => meta.attachments.map(id => ({ id, name: id, mime: 'application/octet-stream', size: 0 })),
    readAttachmentsFor: id => files.get(id) ?? [], writeAttachmentsFor: (id, value) => files.set(id, value),
    loadSessionDraft: id => new Promise((resolve, reject) => loads.push({ id, resolve, reject })),
    saveSessionDraft: async (id, text, ids) => { saves.push({ id, text, ids }); },
    setTimeout: fn => { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
  };
}
async function mounted(f, initial = 'A') {
  const host = document.createElement('div'); document.body.append(host);
  const root = createRoot(host);
  const render = async (id, incognito = false) => React.act(async () => {
    root.render(React.createElement(Harness, { sessionId: id, knobs: { incognito }, fixture: f }));
  });
  await render(initial);
  return { render, close: () => React.act(() => root.unmount()) };
}
async function resolveLoad(f, index, text, updatedAt = Date.now() / 1000 + 100, attachmentIds = []) {
  await React.act(async () => f.loads[index].resolve({ text, updatedAt, attachmentIds }));
}
async function debounce(f) {
  const callbacks = [...f.timers.values()]; f.timers.clear();
  await React.act(async () => { for (const fn of callbacks) fn(); });
}
{
  const f = fixture(), app = await mounted(f);
  assert.equal(f.loads.length, 1, 'direct mount must fetch its server draft');
  await debounce(f);
  assert.equal(f.saves.length, 0, 'pending GET must not be erased by an empty PUT');
  await resolveLoad(f, 0, 'server-only á漢🙂');
  assert.equal(f.screen.draft, 'server-only á漢🙂');
  await debounce(f);
  assert.deepEqual(f.saves, [{ id: 'A', text: 'server-only á漢🙂', ids: [] }]);
  await app.close();
}
{
  const f = fixture(), app = await mounted(f);
  await resolveLoad(f, 0, 'remote files', undefined, ['server-file']);
  assert.deepEqual(f.screen.attachments.map(a => a.id), ['server-file']);
  await debounce(f);
  assert.deepEqual(f.saves, [{ id: 'A', text: 'remote files', ids: ['server-file'] }]);
  await app.close();
}
{
  const f = fixture(), app = await mounted(f);
  await React.act(() => f.screen.setAttachments([{ id: 'new-local-file' }]));
  await resolveLoad(f, 0, 'remote files', undefined, ['server-file']);
  assert.deepEqual(f.screen.attachments.map(a => a.id), ['new-local-file'], 'a slow GET must preserve new uploads');
  await app.close();
}
{
  const f = fixture(); f.texts.set('A', 'newer local'); f.times.set('A', Date.now() + 200000);
  const app = await mounted(f);
  await resolveLoad(f, 0, 'older remote');
  assert.equal(f.screen.draft, 'newer local', 'recency still protects a newer local draft');
  await app.close();
}
{
  const f = fixture(), app = await mounted(f);
  await React.act(() => f.screen.setDraft('typed while GET waits'));
  await resolveLoad(f, 0, 'remote');
  assert.equal(f.screen.draft, 'typed while GET waits', 'a slow GET must preserve new typing');
  await app.close();
}
{
  const f = fixture(); f.texts.set('A', 'draft A'); f.texts.set('B', 'draft B');
  const app = await mounted(f);
  await app.render('B');
  assert.equal(f.texts.get('B'), 'draft B', 'switching must not persist A under B');
  await resolveLoad(f, 0, 'late remote A');
  assert.equal(f.screen.draft, 'draft B', 'cancelled response must not cross chats');
  await resolveLoad(f, 1, 'remote B'); await debounce(f);
  assert.deepEqual(f.saves, [{ id: 'B', text: 'remote B', ids: [] }]);
  await app.close();
}
{
  const f = fixture(), app = await mounted(f);
  await app.render('A', true);
  await resolveLoad(f, 0, 'late remote'); await debounce(f);
  assert.equal(f.screen.draft, '', 'Nobody mode must cancel an outstanding remote draft');
  assert.equal(f.saves.length, 0);
  await app.render(null); await debounce(f);
  assert.equal(f.loads.length, 1, 'an unsaved conversation has no remote draft');
  await app.close();
}
console.log('ok: real Studio effects hydrate, preserve recency and typing, isolate chats and Nobody mode');

if (failed) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}
console.log('ALL OK');
