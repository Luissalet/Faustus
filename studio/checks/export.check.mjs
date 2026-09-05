// Exporting a chat (studio/src/adapters/sessions.ts).
//
// The bug this exists for: `window.open` on the export URL cannot see the
// response, so a 400 (a format the server does not know) or a 503 (the PDF
// dependency is not installed) arrived as a blank tab or a page of raw JSON.
// It reads as "the button is broken", and the server's own sentence — the one
// that says what to install — never reaches anybody.
//
// `fetch`, `URL` and the DOM are stubbed here. Run by
// tests/test_studio_export_js.py, or by hand:
//   node studio/checks/export.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-export-')), 'sessions.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'adapters', 'sessions.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });

// ── the browser, in miniature ──
const clicked = [];
const revoked = [];
globalThis.document = {
  body: { appendChild() {}, removeChild() {} },
  createElement: () => ({
    set href(v) {
      this._href = v;
    },
    get href() {
      return this._href;
    },
    download: '',
    click() {
      clicked.push({ href: this._href, download: this.download });
    },
    remove() {},
  }),
};
globalThis.URL.createObjectURL = () => 'blob:fake';
globalThis.URL.revokeObjectURL = (u) => revoked.push(u);

const s = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

const asked = [];
const serve = ({ ok = true, status = 200, body = {}, headers = {} } = {}) => {
  globalThis.fetch = async (url) => {
    asked.push(url);
    return {
      ok,
      status,
      headers: { get: (k) => headers[k.toLowerCase()] ?? null },
      json: async () => {
        if (typeof body === 'string') throw new SyntaxError('not json');
        return body;
      },
      blob: async () => ({ size: 3 }),
    };
  };
};

// ── The URL keeps the parameter names the server expects ──
{
  assert(s.exportUrl('abc', 'md') === '/api/session/abc/export?fmt=md', 'the single-chat export URL');
  assert(s.EXPORT_FORMATS.includes('pdf') && s.EXPORT_FORMATS.includes('docx'), 'pdf and docx are offered');
  assert(s.EXPORT_FORMATS.length === 6, 'six formats');
}

// ── The server's own message survives a refusal ──
{
  serve({ ok: false, status: 503, body: { detail: 'PDF export needs weasyprint. Install it with: pip install weasyprint' } });
  let msg = '';
  try {
    await s.downloadExport('abc', 'pdf');
  } catch (e) {
    msg = e.message;
  }
  assert(msg.includes('weasyprint'), `a 503 says what to install: ${msg}`);
  assert(clicked.length === 0, 'and nothing is downloaded');

  serve({ ok: false, status: 400, body: { detail: 'unknown format' } });
  msg = '';
  try {
    await s.downloadExport('abc', 'md');
  } catch (e) {
    msg = e.message;
  }
  assert(msg.includes('unknown format'), 'a 400 says why');

  // An error body that is not JSON still has to produce a sentence.
  serve({ ok: false, status: 500, body: '<html>oops</html>' });
  msg = '';
  try {
    await s.downloadExport('abc', 'md');
  } catch (e) {
    msg = e.message;
  }
  assert(msg.includes('500'), `a non-JSON error still says something: ${msg}`);
}

// ── A transport failure is reported, not swallowed ──
{
  globalThis.fetch = async () => {
    throw new Error('network down');
  };
  let msg = '';
  try {
    await s.downloadExport('abc', 'md');
  } catch (e) {
    msg = e.message;
  }
  assert(msg === 'network down', 'a dropped connection is reported as itself');
}

// ── A success saves under the name the server gave ──
{
  clicked.length = 0;
  serve({ headers: { 'content-disposition': "attachment; filename*=UTF-8''Caf%C3%A9%20chat.md" } });
  await s.downloadExport('abc', 'md');
  assert(clicked.length === 1, 'the file is downloaded');
  assert(clicked[0].download === 'Café chat.md', `the server's UTF-8 filename is used: ${clicked[0].download}`);
  assert(clicked[0].href === 'blob:fake', 'from a blob, not by navigating');
}

// ── With no filename from the server, the chat's name is used, made safe ──
{
  clicked.length = 0;
  serve({ headers: {} });
  await s.downloadExport('abc', 'json', 'a/b:c*d?e"f<g>h|i');
  assert(clicked[0].download.endsWith('.json'), 'the format is the extension');
  assert(!/[\\/:*?"<>|]/.test(clicked[0].download), `no character that a filesystem refuses: ${clicked[0].download}`);
}

// ── A plain filename in quotes is read too ──
{
  clicked.length = 0;
  serve({ headers: { 'content-disposition': 'attachment; filename="notes.txt"' } });
  await s.downloadExport('abc', 'txt');
  assert(clicked[0].download === 'notes.txt', `the quoted form is read: ${clicked[0].download}`);
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
