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
import { mkdtempSync } from 'node:fs';
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
if (failed) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}
console.log('ALL OK');
