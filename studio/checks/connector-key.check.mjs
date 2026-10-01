// A server listed on the Connectors screen without a connector record arrives
// with `id: null`. Rows are identified by `connectorKey`, so an absent `?id=`
// never matches one of them (that opened its tools dialog on every visit, and
// the dialog could not be closed), and its tools are read from its MCP server.
//
// Run by tests/test_connector_key_js.py, or by hand:
//   node studio/checks/connector-key.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-conn-')), 'connectors.mjs');
await build({ entryPoints: [join(root, 'studio/src/adapters/connectors.ts')], bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
const c = await import(pathToFileURL(out).href);

const plain = { id: null, server: { id: 'c6d67d7c', name: 'CookHoard local' } };
const managed = { id: 'jobhunter', server: { id: 'abcd1234', name: 'Jobhunter' } };
assert.equal(c.connectorKey(plain), 'server:c6d67d7c');
assert.equal(c.connectorKey(managed), 'jobhunter');
// no ?id= → nothing matches
const drawerId = null;
assert.equal([plain, managed].find((x) => drawerId && c.connectorKey(x) === drawerId), undefined);

const seen = [];
globalThis.fetch = async (url, init) => {
  seen.push([init?.method ?? 'GET', String(url)]);
  const body = String(url).endsWith('/tools') ? [{ name: 'a' }, { name: 'b' }] : { ok: true, tool_count: 2, changed: false };
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
};
const tools = await c.listToolsFor(plain);
assert.equal(tools.length, 2);
assert.ok(seen.at(-1)[1].endsWith('/api/mcp/servers/c6d67d7c/tools'), seen.at(-1)[1]);
await c.listToolsFor(managed);
assert.ok(seen.at(-1)[1].endsWith('/api/app-connectors/jobhunter/tools'), seen.at(-1)[1]);
const r = await c.refreshToolsFor(plain, false);
assert.equal(r.ok, true);
assert.ok(seen.at(-1)[1].endsWith('/api/mcp/servers/c6d67d7c/refresh-tools'), seen.at(-1)[1]);
const rr = await c.refreshToolsFor(plain, true);
assert.equal(rr.reconnected, true);
assert.ok(seen.some(([m, u]) => m === 'POST' && u.endsWith('/api/mcp/servers/c6d67d7c/reconnect')));
console.log('ok connector-key');
