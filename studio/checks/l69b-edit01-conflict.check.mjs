// Lote 69b — EDIT-01: the document editor sends `expected_content` (the
// revision it last read) on every save, and a 409 from the server surfaces
// as `ApiError(status: 409)` for `Editor.tsx`'s conflict banner to catch —
// never a generic failure indistinguishable from a network error, and never
// silently retried in a way that would overwrite someone else's newer save.
//
// Exercises the real `studio/src/adapters/documents.ts::saveDoc` (the exact
// function `Editor.tsx::save()` calls), not a reimplementation.
//
// Run by tests/test_l69b_studio_checks_js.py, or by hand:
//   node studio/checks/l69b-edit01-conflict.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const out = join(mkdtempSync(join(tmpdir(), 'faustus-edit01-')), 'documents.mjs');
await build({ entryPoints: ['studio/src/adapters/documents.ts'], bundle: true, platform: 'node', format: 'esm', outfile: out });
const { saveDoc } = await import(pathToFileURL(out).href);

let failed = 0;
const check = (condition, message) => {
  if (!condition) { failed += 1; console.error('FAIL:', message); }
  else console.log('ok', message);
};

// 1) A normal save sends the caller's expectedContent (Editor.tsx passes
//    `doc.content` — the revision this editor read) as `expected_content`.
{
  let sentBody = null;
  globalThis.fetch = async (path, init) => {
    sentBody = JSON.parse(init.body);
    return new Response(JSON.stringify({ id: 'd1', current_content: 'new text', version_count: 2 }), { status: 200 });
  };
  const saved = await saveDoc('d1', 'new text', 'a summary', false, 'old text');
  check(sentBody.expected_content === 'old text', 'saveDoc sends the last-read revision as expected_content');
  check(sentBody.force_version === false, 'a plain save does not force a new version');
  check(saved.content === 'new text', 'the saved doc reflects the server response');
}

// 2) A conflicting save (server content moved on) answers 409 — saveDoc
//    must throw an ApiError carrying that status, not swallow it as a plain
//    Error, so Editor.tsx's `e instanceof ApiError && e.status === 409`
//    branch (the recoverable-conflict banner) actually fires.
{
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'Document changed. Your draft was not saved; review the latest version.' }), { status: 409 });
  let caught = null;
  try {
    await saveDoc('d1', 'my edit', undefined, false, 'stale base');
  } catch (e) {
    caught = e;
  }
  check(caught !== null, 'a 409 response makes saveDoc throw');
  check(caught && caught.status === 409, 'the thrown error carries status 409 (ApiError), not a bare Error');
  check(caught && /changed/i.test(caught.message), "the server's detail message is preserved for the banner");
}

// 3) A force-save (the conflict banner's "Force-save mine" action) omits
//    expected_content entirely — no client-side base revision to compare —
//    so the write goes through regardless of what is on the server now.
{
  let sentBody = null;
  globalThis.fetch = async (path, init) => {
    sentBody = JSON.parse(init.body);
    return new Response(JSON.stringify({ id: 'd1', current_content: 'forced', version_count: 3 }), { status: 200 });
  };
  await saveDoc('d1', 'forced', undefined, true);
  check(sentBody.expected_content === undefined, 'a forced save omits expected_content — nothing left to conflict on');
  check(sentBody.force_version === true, 'a forced save also forces a new version, not a coalesced one');
}

if (failed) {
  console.error(`${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('ALL OK: saveDoc carries the read revision, surfaces a 409 as ApiError for the conflict banner, and a forced save bypasses the check');
