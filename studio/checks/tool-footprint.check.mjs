// Tool footprint adapter (studio/src/adapters/toolFootprint.ts) — OBJ-56 UI.
// Run by tests/test_tool_footprint_js.py, or by hand:
//   node studio/checks/tool-footprint.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-tool-footprint-')), 'toolFootprint.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'adapters', 'toolFootprint.ts')],
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

const sample = {
  model: '',
  count: 2,
  total_tokens: 100,
  offered_tokens: 80,
  deferred_tokens: 20,
  hidden_tokens: 0,
  hidden_tools: 0,
  by_source: [
    { source: 'builtin', tools: 1, tokens: 60, share: 0.6 },
    { source: 'email', tools: 1, tokens: 40, share: 0.4 },
  ],
  heaviest: [],
  oversized_descriptions: [{ name: 'heavy', source: 'builtin', description_tokens: 300 }],
  name_collisions: [
    { short_name: 'search', tools: ['search', 'mcp__email__search'], sources: ['builtin', 'email'], tokens: 40 },
  ],
  near_duplicates: [{ a: 'a', b: 'b', similarity: 0.8, same_source: false, tokens: 10 }],
  thresholds: { similarity: 0.6, oversized_description_tokens: 250 },
  fingerprint: 'abc',
};

{
  calls.length = 0;
  reply(sample);
  const d = await mod.loadToolFootprint();
  assert(calls[0].url === '/api/tools/footprint', `bare path (got ${calls[0].url})`);
  assert(calls[0].method === 'GET', 'footprint is a GET');
  assert(d.count === 2 && d.by_source[0].source === 'builtin', 'typed report comes back');
  assert(d.oversized_descriptions[0].description_tokens === 300, 'oversized rows typed');
}

{
  calls.length = 0;
  reply(sample);
  await mod.loadToolFootprint({ source: 'email', top: 5, similarity: 0.7 });
  assert(
    calls[0].url === '/api/tools/footprint?source=email&top=5&similarity=0.7',
    `filters encoded (got ${calls[0].url})`,
  );
}

{
  calls.length = 0;
  reply(sample);
  await mod.loadToolFootprint({ source: '', top: undefined });
  assert(calls[0].url === '/api/tools/footprint', `empty filters dropped (got ${calls[0].url})`);
}

assert(mod.footprintSourceLabel('builtin') === 'Built-in', 'builtin label');
assert(mod.footprintSourceLabel('email') === 'email', 'mcp source passthrough');

// UI wiring: Settings → Tools mounts the footprint panel.
const toolsTsx = readFileSync(join(root, 'studio', 'src', 'screens', 'settings', 'Tools.tsx'), 'utf8');
assert(toolsTsx.includes('ToolFootprintPanel'), 'Tools.tsx imports/mounts ToolFootprintPanel');
assert(
  existsSync(join(root, 'studio', 'src', 'screens', 'settings', 'ToolFootprint.tsx')),
  'ToolFootprint.tsx exists',
);
const panel = readFileSync(join(root, 'studio', 'src', 'screens', 'settings', 'ToolFootprint.tsx'), 'utf8');
assert(panel.includes('data-testid="tool-footprint"'), 'panel has data-testid');
assert(panel.includes('loadToolFootprint'), 'panel calls the adapter');

if (failed) {
  console.error(`${failed} failure(s)`);
  process.exit(1);
}
console.log('tool-footprint.check.mjs: all ok');
