// BENCH-03 - evidence adapter (studio/src/adapters/evidence.ts): shapes the
// wire response into EvidenceResolution without ever conflating captured
// and current content.
//
// Bundled with esbuild on the fly; run by tests/test_p1_bench03_evidence_js.py,
// or by hand:
//   node studio/checks/p1-bench03-evidence.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-evidence-'));
async function load(rel, name) {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', rel)], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const m = await load(join('adapters', 'evidence.ts'), 'evidence.mjs');

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };

const EVIDENCE = { schema_version: '1.0', evidence_id: 'evi_1', owner_id: 'admin', project_id: null,
  source_type: 'file', source_ref: 'a.txt', source_revision: 'abc', content_sha256: 'deadbeef',
  captured_at: '2026-01-01T00:00:00Z', locator: { kind: 'lines', value: '1-3' }, derived_from: [], retention: 'task' };

const originalFetch = globalThis.fetch;
let lastRequest = null;
globalThis.fetch = async (url, init) => {
  lastRequest = { url, init };
  return new Response(JSON.stringify({
    evidence: EVIDENCE, current_available: true, current_content: 'fresh text',
    still_valid: false, reason: 'The file changed.',
  }), { status: 200, headers: { 'Content-Type': 'application/json' } });
};

try {
  const r = await m.resolveEvidence(EVIDENCE, '/repo');
  assert(lastRequest.url === '/api/evidence/resolve', 'posts to the resolver endpoint');
  const body = JSON.parse(lastRequest.init.body);
  assert(body.evidence.evidence_id === 'evi_1', 'sends the evidence mapping as-is');
  assert(body.workspace === '/repo', 'sends the workspace when given');
  assert(r.stillValid === false, 'stillValid decoded');
  assert(r.currentContent === 'fresh text', 'currentContent decoded — this is explicitly the FRESH read, never the captured one');
  assert(r.reason === 'The file changed.', 'reason decoded');

  // ── a refusal (e.g. 403) surfaces as a real error, not a silently empty result ──
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'nope' }), { status: 403 });
  let threw = false;
  try { await m.resolveEvidence(EVIDENCE); } catch (e) { threw = true; assert(e.status === 403, 'ApiError carries the status'); }
  assert(threw, 'a refused resolve throws rather than returning something that looks like success');
} finally {
  globalThis.fetch = originalFetch;
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
