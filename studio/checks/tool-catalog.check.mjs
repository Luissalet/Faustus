// Tool catalog adapter (studio/src/adapters/tools.ts) — TOOL-01, TOOL-03
// partial. Exercises listToolCatalog/getToolDescriptor/dryRunTool against a
// stubbed `fetch`, and the per-viewer favorites store against a stubbed
// `localStorage` (including the "storage is unavailable" path — private
// browsing, or a thumbnail-capture context with no site data).
//
// Run by tests/test_tool_registry.py's frontend leg, or by hand:
//   node studio/checks/tool-catalog.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-tool-catalog-')), 'tools.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'adapters', 'tools.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const tools = await import(pathToFileURL(out).href);

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
    calls.push({ url, method: init?.method ?? 'GET', body: init?.body });
    return {
      ok,
      status,
      json: async () => body,
      clone() {
        return this;
      },
    };
  };
}

// ── listToolCatalog: builds the query string, only for the filters given ──
{
  calls.length = 0;
  reply({ checked_at: 'now', fingerprint: 'abc', count: 1, tools: [{ name: 'bash', version: '1.0.0', description: 'run', effect_class: 'execute', required_scopes: ['execute_code'], timeout_ms: 300000, cancellation: 'best_effort', idempotency: 'not_supported', max_output_bytes: 10000, executor: 'native', mcp: null }] });
  const d = await tools.listToolCatalog();
  assert(calls[0].url === '/api/tools/catalog', `no filter: bare path (got ${calls[0].url})`);
  assert(d.tools[0].name === 'bash', 'the row comes back typed');

  calls.length = 0;
  await tools.listToolCatalog({ q: 'web search', executor: 'native' });
  assert(calls[0].url === '/api/tools/catalog?q=web%20search&executor=native', `both filters encoded (got ${calls[0].url})`);

  calls.length = 0;
  await tools.listToolCatalog({ q: '' });
  assert(calls[0].url === '/api/tools/catalog', `an empty filter is dropped, not sent as q= (got ${calls[0].url})`);
}

// ── getToolDescriptor ──
{
  calls.length = 0;
  reply({ checked_at: 'now', tool: { name: 'bash', input_schema: { type: 'object' } }, mcp: null });
  const d = await tools.getToolDescriptor('bash');
  assert(calls[0].url === '/api/tools/catalog/bash', 'fetches the one-tool route');
  assert(d.tool.name === 'bash', 'the descriptor comes back');
}

{
  calls.length = 0;
  reply({ checked_at: 'now', tool: { name: 'weird/name' }, mcp: null });
  await tools.getToolDescriptor('weird/name');
  assert(calls[0].url === '/api/tools/catalog/weird%2Fname', 'the tool name is URL-encoded');
}

// ── dryRunTool: POSTs {arguments}, never a GET, and surfaces the backend's own message on failure ──
{
  calls.length = 0;
  reply({ tool: 'read_file', schema_available: true, ok: false, errors: [{ field: 'limit', kind: 'wrong_type', detail: 'expected integer, saw str', seen: '90' }], repairs: [{ field: 'limit', from: '90', to: 90, reason: 'numeric string coerced' }], repaired_arguments: { limit: 90 }, remaining_errors: [] });
  const d = await tools.dryRunTool('read_file', { path: 'x.txt', limit: '90' });
  assert(calls[0].method === 'POST', 'dry-run is a POST');
  assert(calls[0].url === '/api/tools/catalog/read_file/dry-run', 'to the right route');
  assert(JSON.parse(calls[0].body).arguments.limit === '90', 'the raw arguments are sent as-is, uncorrected');
  assert(d.ok === false && d.repaired_arguments.limit === 90, 'the typed result comes back');
}

{
  reply({ detail: "No tool named 'nope' in the catalogue" }, false, 404);
  let message = '';
  try {
    await tools.dryRunTool('nope', {});
  } catch (e) {
    message = e.message;
  }
  assert(message === "No tool named 'nope' in the catalogue", `the backend's own message survives, got: ${message}`);
}

// ── favorites: try/catch around localStorage, both directions ──
{
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, v),
  };
  assert(tools.loadFavoriteTools().size === 0, 'no favorites yet: empty set, not a throw');
  tools.saveFavoriteTools(new Set(['bash', 'read_file']));
  const loaded = tools.loadFavoriteTools();
  assert(loaded.has('bash') && loaded.has('read_file') && loaded.size === 2, 'round-trips through localStorage');
}

{
  globalThis.localStorage = {
    getItem() {
      throw new Error('storage disabled');
    },
    setItem() {
      throw new Error('storage disabled');
    },
  };
  let threw = false;
  try {
    tools.loadFavoriteTools();
    tools.saveFavoriteTools(new Set(['bash']));
  } catch {
    threw = true;
  }
  assert(!threw, 'a storage failure (private browsing, thumbnail capture) never throws out of the adapter');
}

{
  globalThis.localStorage = { getItem: () => 'not json', setItem() {} };
  assert(tools.loadFavoriteTools().size === 0, 'unparseable stored value: empty set, not a throw');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
