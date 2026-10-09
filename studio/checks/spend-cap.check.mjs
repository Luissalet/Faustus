// Daily paid spend cap (src/spend_cap.py), Studio side, checked without a browser:
//   1. the adapter reads GET /api/spend-cap and passes the body through;
//   2. the Settings card's share-of-limit helper treats limit 0 as "no limit"
//      (null) and never exceeds 100.
// Run by tests/test_spend_cap.py, or by hand: node studio/checks/spend-cap.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const dir = mkdtempSync(join(tmpdir(), 'faustus-spendcap-'));
const adapterOut = join(dir, 'settings.mjs');
await build({ entryPoints: ['studio/src/adapters/settings.ts'], bundle: true, platform: 'node', format: 'esm', outfile: adapterOut });
const cardOut = join(dir, 'card.mjs');
await build({
  entryPoints: ['studio/src/screens/settings/SpendCap.tsx'], bundle: true, platform: 'node', format: 'esm',
  outfile: cardOut, jsx: 'automatic',
});

let requested = null;
const body = { enabled: true, limit_usd: 5, limit_tokens: 0, spent_usd: 1.25, tokens_today: 10, by_provider: [] };
globalThis.fetch = async (url) => {
  requested = String(url);
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
};
const { getSpendCap } = await import(pathToFileURL(adapterOut).href);
const got = await getSpendCap();
assert.equal(requested, '/api/spend-cap');
assert.equal(got.spent_usd, 1.25);

const { capShare } = await import(pathToFileURL(cardOut).href);
assert.equal(capShare(1, 0), null);
assert.equal(capShare(1.25, 5), 25);
assert.equal(capShare(9, 5), 100);
console.log('spend-cap check ok');
