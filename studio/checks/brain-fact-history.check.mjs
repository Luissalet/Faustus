// Fact change-chain adapter (studio/src/adapters/brainFactHistory.ts) - OBJ-60 part 2.
// Run by tests/test_brain_fact_history_js.py, or by hand:
//   node studio/checks/brain-fact-history.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-fact-history-')), 'brainFactHistory.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'adapters', 'brainFactHistory.ts')],
  bundle: true,
  format: 'esm',
  platform: 'node',
  outfile: out,
  logLevel: 'silent',
});
const mod = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

const calls = [];
function reply(body, ok = true, status = 200) {
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return { ok, status, json: async () => body, clone() { return this; }, text: async () => JSON.stringify(body) };
  };
}

const body = {
  subject: { id: 'e1', name: 'Ada' },
  attribute: 'uses',
  current: [{ id: 'r2', value: 'Helix', valid_from: '2026-10-01T00:00:00Z', valid_until: '', status: 'active', supersedes: 'r1', superseded_by: '', source: 'mem:b', source_session: 'sess-b' }],
  events: [
    { id: 'r1', value: 'Vim', valid_from: '2026-01-01T00:00:00Z', valid_until: '2026-10-01T00:00:00Z', status: 'superseded', supersedes: '', superseded_by: 'r2', source: 'mem:a', source_session: 'sess a/1' },
    { id: 'r2', value: 'Helix', valid_from: '2026-10-01T00:00:00Z', valid_until: '', status: 'active', supersedes: 'r1', superseded_by: '', source: 'mem:b', source_session: 'sess-b' },
  ],
  history_line: 'antes: Vim, hasta 2026-10',
  changes: 2,
};

assert(mod.factHistoryUrl('e 1', 'works at') === '/api/brain/fact-history?subject=e%201&attribute=works%20at&limit=20', 'URL is encoded');

reply(body);
const h = await mod.loadFactHistory('e1', 'uses');
assert(calls[0].url === '/api/brain/fact-history?subject=e1&attribute=uses&limit=20', 'calls the endpoint');
assert(h.subjectName === 'Ada' && h.attribute === 'uses' && h.changes === 2, 'maps subject, attribute, changes');
assert(h.current.length === 1 && h.current[0].value === 'Helix', 'current value');
assert(h.previous.length === 1 && h.previous[0].value === 'Vim' && h.previous[0].validUntil.startsWith('2026-10'), 'previous values exclude the current one');
assert(mod.sourceHref(h.previous[0]) === '/studio?s=sess%20a%2F1', 'source links to the session');
assert(mod.sourceHref({ ...h.previous[0], sourceSession: '' }) === null, 'no session, no link');

const empty = mod.factHistoryFrom({});
assert(empty.changes === 0 && empty.previous.length === 0 && empty.current.length === 0, 'empty body is safe');
assert(mod.factHistoryFrom({ events: [{ id: 'x', value: '' }], current: [] }).previous.length === 0, 'blank values are skipped');

reply({ detail: 'unknown subject' }, false, 400);
let err = null;
try { await mod.loadFactHistory('zz', 'uses'); } catch (e) { err = e; }
assert(err && err.status === 400, 'an HTTP error surfaces as ApiError');

const panel = readFileSync(join(root, 'studio', 'src', 'screens', 'brain', 'EntityPanel.tsx'), 'utf8');
assert(panel.includes('loadFactHistory') && panel.includes('<FactChains'), 'EntityPanel renders the change chains');

if (failed) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}
console.log('all brain fact-history checks passed');
