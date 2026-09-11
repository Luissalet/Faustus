// Lote 70a, punto A.15 (Studio half): `adapters/context.ts::loadManifestItemFragment`
// (GET /api/context/packets/{packet_id}/items/{item_id}/fragment, the route
// half tested at `tests/test_l70_a15_manifest_item_fragment_route.py`) and
// the "Ver fragmento" button `Context.tsx::ManifestPane` offers off it.
//
// Run by tests/test_l70_a15_manifest_fragment_button_js.py, or by hand:
//   node studio/checks/l70-a15-manifest-fragment-button.check.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-a15-fragment-')), 'context.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/context.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
const { loadManifestItemFragment } = await import(pathToFileURL(out).href);

let failed = 0;
const check = (cond, msg) => { if (!cond) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };
const originalFetch = globalThis.fetch;

// ── a resolvable fragment ──
{
  let seenPath;
  globalThis.fetch = async (path) => { seenPath = path; return new Response(JSON.stringify({ ok: true, packet_id: 'p1', item_id: 'item1', resolvable: true, text: 'the exact text' }), { status: 200 }); };
  const frag = await loadManifestItemFragment('p1', 'item1');
  check(seenPath === '/api/context/packets/p1/items/item1/fragment', 'loadManifestItemFragment() hits GET .../items/{item_id}/fragment');
  check(frag.resolvable === true, 'loadManifestItemFragment() decodes resolvable: true');
  check(frag.text === 'the exact text', 'loadManifestItemFragment() decodes text');
}

// ── a clean miss (unresolvable) ──
{
  globalThis.fetch = async () => new Response(JSON.stringify({ ok: true, resolvable: false, text: '' }), { status: 200 });
  const frag = await loadManifestItemFragment('p1', 'item2');
  check(frag.resolvable === false, 'loadManifestItemFragment() decodes resolvable: false as a clean miss, not a throw');
}

// ── an evicted manifest's honest-miss note ──
{
  globalThis.fetch = async () => new Response(JSON.stringify({ ok: true, resolvable: false, text: '', note: 'evicted' }), { status: 200 });
  const frag = await loadManifestItemFragment('p1', 'item3');
  check(frag.note === 'evicted', 'loadManifestItemFragment() decodes the note');
}

// ── a 404 (unknown packet/item) rejects ──
{
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'No such packet' }), { status: 404 });
  let threw = false;
  try { await loadManifestItemFragment('nope', 'item1'); } catch { threw = true; }
  check(threw, 'loadManifestItemFragment() rejects on a 404');
}
globalThis.fetch = originalFetch;

// ── Context.tsx: the button and inline fragment viewer are actually wired ──
{
  const src = readFileSync(join(root, 'studio/src/screens/Context.tsx'), 'utf-8');
  check(src.includes('loadManifestItemFragment'), 'Context.tsx imports loadManifestItemFragment');
  check(src.includes('data-testid="context-manifest-fragment-btn"'), 'the "Ver fragmento" button has a stable test id');
  check(src.includes('item.sourceRef &&'), 'the button is gated on the row actually having a source_ref');
  check(src.includes('data-testid="context-manifest-fragment"'), 'the opened fragment row has a stable test id');
  check(src.includes('fragment.resolvable === false'), 'an unresolvable fragment renders its own (non-error) state');
}

if (failed) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}
console.log('ALL OK');
