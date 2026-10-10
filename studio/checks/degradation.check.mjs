// Agora #202: the degradation readout (studio/src/adapters/degradation.ts) --
// reading the server's report, keeping only degraded/down items for the
// banner, and the wiring of the Vitals section (adapter, Spanish rows).
// Run by tests/test_studio_degradation_js.py, or by hand:
//   node studio/checks/degradation.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-degr-')), 'degradation.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'adapters', 'degradation.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const d = await import(pathToFileURL(out).href);
const read = (p) => readFileSync(join(root, p), 'utf8').replace(/\r\n/g, '\n');

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};
const eq = (x, y, msg) => assert(JSON.stringify(x) === JSON.stringify(y), `${msg} (got ${JSON.stringify(x)})`);

const raw = {
  overall: 'down',
  generated_at: '2026-10-10T10:00:00+00:00',
  items: [
    { id: 'searxng', label: 'Web search', state: 'down', cause: 'ConnectionRefusedError: connection refused', since: '2026-10-10T09:00:00+00:00', last_checked: 'x', latency_ms: 12, fix_hint: { es: 'arranca', en: 'start', command: 'docker compose up -d searxng' }, affects: ['web search', 'deep research'] },
    { id: 'email', label: 'Email', state: 'ok', cause: '', since: 's', last_checked: 'x', latency_ms: 3, fix_hint: { es: '', en: '', command: '' }, affects: [] },
    { id: 'ntfy', label: 'Notifications', state: 'unconfigured', cause: 'none', since: 's', last_checked: 'x', latency_ms: 1, fix_hint: {}, affects: [] },
    { id: 'mcp_servers', label: 'MCP servers', state: 'degraded', cause: 'x', since: 's', last_checked: 'x', latency_ms: null, fix_hint: { es: 'a', en: 'b', command: '' }, affects: ['tools'] },
  ],
};
const r = d.parseDegradation(raw);
eq(r.problems.map((p) => p.id), ['searxng', 'mcp_servers'], 'only degraded and down items are problems');
eq(r.unconfigured, 1, 'unconfigured is counted, not shown');
eq(r.problems[0].hint, { es: 'arranca', en: 'start', command: 'docker compose up -d searxng' }, 'fix hint keeps both languages and the command');
eq(r.problems[0].affects, ['web search', 'deep research'], 'affected features are kept');
eq(r.problems[1].latencyMs, null, 'missing latency stays null');
eq(d.parseDegradation({}).problems, [], 'an empty answer has no problems');
eq(d.parseDegradation({ items: [{ id: 'z', state: 'weird' }] }).problems.map((p) => p.state), ['degraded'], 'an unknown state is never read as ok');

const vitals = read('studio/src/screens/studio/Vitals.tsx');
assert(vitals.includes("from '../../adapters/degradation'") && vitals.includes('<DegradationSection />'), 'Vitals renders the degradation section');
const tsv = read('docs/ui/i18n/es.tsv');
const es = read('studio/src/i18n/es.ts');
for (const k of ['Running degraded', 'Since {when}', 'Affects: {list}', '{n} dependency', '{n} dependencies']) {
  assert(tsv.includes(k + '\t') && es.includes(JSON.stringify(k)), `Spanish row for "${k}"`);
}

if (failed) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}
console.log('ALL OK');
