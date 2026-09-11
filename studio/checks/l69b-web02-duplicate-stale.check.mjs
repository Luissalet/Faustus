// Lote 69b — WEB-02: `studio/src/adapters/research.ts`'s `sourceFrom` (shared
// by `researchDetail` and `resultFrom`) parses `duplicate_of`/`stale`/
// `age_days` off a source when the backend stamps them
// (`src/deep_research.py::_stamp_duplicate`) — and stays silent (no flags)
// when it doesn't, since today's `src/research_handler.py::_extract_sources`
// still strips them before the wire (see this lote's report). Once that
// backend passthrough lands, no frontend change is needed — these fields
// already parse.
//
// Run by tests/test_l69b_studio_checks_js.py, or by hand:
//   node studio/checks/l69b-web02-duplicate-stale.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';

async function bundle(entry) {
  const result = await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', write: false, logLevel: 'silent' });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

const { researchResult } = await bundle('studio/src/adapters/research.ts');

let failed = 0;
const check = (condition, message) => {
  if (!condition) { failed += 1; console.error('FAIL:', message); }
  else console.log('ok', message);
};

globalThis.fetch = async () => new Response(JSON.stringify({
  result: 'The report text',
  category: 'general',
  sources: [
    { url: 'https://a.example/1', title: 'First' },
    { url: 'https://a.example/2', title: 'Same content, another URL', duplicate_of: 'https://a.example/1' },
    { url: 'https://a.example/3', title: 'Cached copy', stale: true, age_days: 42.7 },
    { url: 'https://a.example/4', title: 'Fresh, no signal at all' },
  ],
  raw_findings: [],
}), { status: 200 });

const r = await researchResult('job-1');

check(r.sources[0].duplicateOf === undefined, 'a source with no duplicate_of parses as undefined, not a fabricated value');
check(r.sources[1].duplicateOf === 'https://a.example/1', 'duplicate_of parses to the earlier URL it names');
check(r.sources[2].stale === true, 'stale parses as a real boolean');
check(r.sources[2].ageDays === 42.7, 'age_days parses as a number, kept as-is (the UI rounds for display)');
check(r.sources[3].stale === undefined, 'no stale signal parses as undefined ("no signal"), never a guessed false/fresh');
check(r.sources[3].ageDays === undefined, 'no age_days parses as undefined');

if (failed) {
  console.error(`${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('ALL OK: research.ts parses duplicate_of/stale/age_days when the backend stamps them, and stays silent when it does not');
