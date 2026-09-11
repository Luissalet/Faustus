// Lote 70a, punto A.13: IDX-04's `status`/`partial`/`partial_reason`
// (`src/upload_handler.py::save_upload`, via `pdf_ingestion_signal`) now
// reach the client — `routes/upload_routes.py`'s `/api/upload` response
// forwards them (lote 70a) and `adapters/composer.ts::uploadFiles()` decodes
// them onto `Attachment.status`/`.partial`/`.partialReason`, so
// `Composer.tsx` can warn on a scanned/cover-only PDF instead of showing it
// indistinguishable from a normally-read one.
//
// Run by tests/test_l70_a13_attachment_partial_status_js.py, or by hand:
//   node studio/checks/l70-a13-attachment-partial-status.check.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-a13-partial-')), 'composer.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/composer.ts')], bundle: true,
  platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
const { uploadFiles } = await import(pathToFileURL(out).href);

let failed = 0;
const check = (cond, msg) => { if (!cond) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };
const originalFetch = globalThis.fetch;

async function uploadWith(fileEntry) {
  globalThis.fetch = async () => new Response(JSON.stringify({ files: [fileEntry] }), { status: 200 });
  try {
    return await uploadFiles([new File(['x'], 'x.pdf')]);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

// ── a normally-read PDF: ready, not partial ──
{
  const [a] = await uploadWith({ id: 'f1', name: 'doc.pdf', mime: 'application/pdf', size: 10, status: 'ready', partial: false, partial_reason: null });
  check(a.status === 'ready', 'uploadFiles() decodes status: ready');
  check(a.partial === false, 'uploadFiles() decodes partial: false');
  check(a.partialReason === undefined, 'uploadFiles() leaves partialReason undefined when null');
}

// ── a scanned PDF ──
{
  const [a] = await uploadWith({ id: 'f2', name: 'scan.pdf', mime: 'application/pdf', size: 10, status: 'partial', partial: true, partial_reason: 'scanned' });
  check(a.status === 'partial', 'uploadFiles() decodes status: partial');
  check(a.partial === true, 'uploadFiles() decodes partial: true');
  check(a.partialReason === 'scanned', 'uploadFiles() decodes partial_reason: scanned');
}

// ── a cover-only PDF ──
{
  const [a] = await uploadWith({ id: 'f3', name: 'cover.pdf', mime: 'application/pdf', size: 10, status: 'partial', partial: true, partial_reason: 'cover_only' });
  check(a.partialReason === 'cover_only', 'uploadFiles() decodes partial_reason: cover_only');
}

// ── an older server that never sends these fields at all ──
{
  const [a] = await uploadWith({ id: 'f4', name: 'plain.txt', mime: 'text/plain', size: 10 });
  check(a.status === undefined, 'uploadFiles() leaves status undefined against an older server');
  check(a.partial === false, 'uploadFiles() defaults partial to false against an older server');
}

// ── Composer.tsx: actually renders the warning off Attachment.partial ──
{
  const src = readFileSync(join(root, 'studio/src/screens/studio/Composer.tsx'), 'utf-8');
  check(src.includes('a.partial'), 'Composer.tsx reads a.partial');
  check(src.includes("a.partialReason === 'scanned'"), 'Composer.tsx distinguishes the scanned reason');
  check(src.includes("a.partialReason === 'cover_only'"), 'Composer.tsx distinguishes the cover_only reason');
  check(src.includes('data-testid="studio-attachment-partial"'), 'the partial warning has a stable test id');
}

if (failed) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}
console.log('ALL OK');
