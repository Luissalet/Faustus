// Schema extraction adapter — fixtures mirror real API shapes from root CAS1
// (research-inbox/extraction66-root-contract.json).
// Run: node studio/checks/extraction.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-extraction-'));

async function load(rel, name) {
  const out = join(dir, name);
  await build({
    entryPoints: [join(root, 'studio', 'src', rel)],
    bundle: true,
    format: 'esm',
    platform: 'node',
    outfile: out,
    logLevel: 'silent',
  });
  return import(pathToFileURL(out).href);
}

const ex = await load(join('adapters', 'extraction.ts'), 'extraction.mjs');

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

// Real limits list from API
const limits = ex.limitsFrom([{ code: 'lexical_check_only', note: 'Grounding is lexical' }]);
assert(limits && limits.message.includes('Grounding') && limits.items[0].code === 'lexical_check_only', 'limits list {code,note}');

// unit integer kept (not coerced empty)
const page = ex.evidenceFrom([{ path: 'amount', quote: '12 EUR', unit: 1 }]);
assert(page[0].unit === '1', 'evidence.unit integer preserved');

// nested paths use dot / bracket like the API
const nested = ex.displayDataRows(ex.resultFrom({
  data: { nested: { a: 12 }, lines: [{ amount: 3 }] },
  evidence: [
    { path: 'nested.a', quote: '12', unit: 1 },
    { path: 'lines[0].amount', quote: '3', unit: 2 },
  ],
  inferred: ['nested.a'],
  dropped: [{ path: 'lines[0].tax', value: null, why: 'missing', code: 'missing' }],
  missing_required: [],
  conflicts: [{ path: 'nested.a', why: 'two quotes' }],
  errors: ['repair failed once'],
  schema_valid: true,
  limits: [{ code: 'lexical_check_only', note: 'Grounding is lexical' }],
  route: { tier: 'simple', purpose: 'utility', model: 'big-extractor', reasons: ['2 fields'] },
  source: 'text',
}));
assert(nested.some((r) => r.path === 'nested.a' && r.evidence && r.evidence.quote === '12' && r.inferred), 'nested.a path+evidence+inferred');
assert(nested.some((r) => r.path === 'lines[0].amount' && r.evidence && r.evidence.unit === '2'), 'lines[0].amount path');
assert(!nested.some((r) => r.path === 'nested/a' || r.path === 'lines/0/amount'), 'no slash paths');

const full = ex.resultFrom({
  data: { total: 12 },
  evidence: [{ path: 'total', quote: '12', unit: 1 }],
  dropped: [{ path: 'bad_sign', value: '−120', why: 'sign ambiguous', code: 'sign_ambiguous' }],
  inferred: [],
  missing_required: [],
  conflicts: ['x'],
  errors: ['the model answer does not fit'],
  schema_valid: true,
  limits: [{ code: 'lexical_check_only', note: 'Grounding is lexical' }],
  route: { purpose: 'utility', model: 'small-extractor', tier: 'simple' },
});
assert(full.limits && full.limits.items.length === 1, 'result limits from list');
assert(full.route.includes('utility') && full.route.includes('small-extractor'), 'route object summarized');
assert(full.errors.length === 1 && full.conflicts.length === 1, 'errors and conflicts kept');
assert(!ex.displayDataRows(full).some((r) => r.path === 'bad_sign'), 'dropped hidden');

const profile = ex.profileFrom({
  profile: { tier: 'simple', reasons: ['1 fields, depth 1, no arrays of objects, no $ref'], leaves: 1 },
  route: { tier: 'simple', purpose: 'utility', model: null, reasons: ['1 fields'] },
});
assert(profile.complexity === 'simple' && profile.summary.includes('1 fields'), 'profile objects → summary');

const poisoned = ex.resultFrom({
  data: { 'nested.a': 1, ok: 1 },
  dropped: [{ path: 'nested.a', value: 1, why: 'x', code: 'x' }],
  evidence: [],
  inferred: [],
  missing_required: [],
  schema_valid: false,
});
assert(!ex.displayDataRows(poisoned).some((r) => r.path === 'nested.a'), 'dropped nested.a hidden even if in data');

const bad = ex.parseSchemaText('{');
assert(bad.ok === false, 'invalid JSON refused');
const good = ex.parseSchemaText('{"type":"object","properties":{}}');
assert(good.ok === true, 'object schema accepted');
assert(ex.schemaNameOk('Invoice_1') && !ex.schemaNameOk('-bad') && !ex.schemaNameOk(''), 'schema name rules');

if (failed) {
  console.error(`FAILED ${failed}`);
  process.exit(1);
}
console.log('ALL OK');
