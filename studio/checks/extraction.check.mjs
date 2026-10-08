// Schema extraction adapter reasoning (studio/src/adapters/extraction.ts).
//
// A dropped value must never be shown as extracted data. Limits stay warnings.
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

const sample = ex.resultFrom({
  data: { total: 12, currency: 'EUR', nested: { a: 1 } },
  evidence: [{ path: 'total', quote: '12', unit: 'page:1' }],
  dropped: [{ path: 'bad_sign', value: '−120', why: 'sign ambiguous', code: 'sign_ambiguous' }],
  inferred: ['currency'],
  missing_required: ['invoice_id'],
  conflicts: [],
  schema_valid: true,
  limits: { message: 'lexical only', detail: 'does not prove field membership' },
  route: 'extraction',
  source: 'text',
});

assert(sample.dropped.length === 1 && sample.dropped[0].code === 'sign_ambiguous', 'dropped keeps why/code');
assert(sample.missingRequired.includes('invoice_id'), 'missing_required mapped');
assert(sample.limits && sample.limits.message.includes('lexical'), 'limits preserved as warning');

const rows = ex.displayDataRows(sample);
assert(rows.every((r) => r.path !== 'bad_sign'), 'dropped path never appears in display rows');
assert(rows.some((r) => r.path === 'total' && r.evidence && r.evidence.quote === '12'), 'valid field keeps evidence');
assert(rows.some((r) => r.path === 'currency' && r.inferred === true), 'inferred flag set');
assert(rows.some((r) => r.path === 'nested/a'), 'nested paths flattened');

// If the server mistakenly also put a dropped path under data, still hide it.
const poisoned = ex.resultFrom({
  data: { bad_sign: '−120', ok: 1 },
  dropped: [{ path: 'bad_sign', value: '−120', why: 'sign', code: 'sign_ambiguous' }],
  evidence: [],
  inferred: [],
  missing_required: [],
  schema_valid: false,
});
assert(!ex.displayDataRows(poisoned).some((r) => r.path === 'bad_sign'), 'poisoned data path still hidden when dropped');
assert(ex.displayDataRows(poisoned).some((r) => r.path === 'ok'), 'non-dropped data remains');

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
