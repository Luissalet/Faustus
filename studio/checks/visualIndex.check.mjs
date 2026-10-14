// Visual index adapter reasoning (studio/src/adapters/visualIndex.ts).
//
// The screen must never claim more than the server said: no vision model means the strip is "not read".
// Run: node studio/checks/visualIndex.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-visual-index-'));

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

const vi = await load(join('adapters', 'visualIndex.ts'), 'visualIndex.mjs');

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

assert(
  JSON.stringify(vi.parseSources('a.pdf\n https://x.org/p , "b.png"\n\na.pdf')) === JSON.stringify(['a.pdf', 'https://x.org/p', 'b.png']),
  'sources: one per line or comma, trimmed, quotes removed, duplicates dropped',
);
assert(vi.isUrl('https://x.org') && !vi.isUrl('/home/a.pdf'), 'url detection');

const missing = vi.statusFrom({
  enabled: true,
  embedder: { embedder: 'Qwen/Qwen3-VL-Embedding-2B', dependencies_missing: ['torch', 'transformers'], install_hint: 'pip install torch transformers', model_downloaded: false },
  vision: { available: false, note: 'no vision model is running' },
  collections: [{ collection: 'docs', sources: 2, strips: 14, embedder: 'x' }],
});
assert(vi.readiness(missing).ready === false && vi.readiness(missing).reason === 'dependencies', 'missing dependencies block indexing');
assert(missing.collections[0].name === 'docs' && missing.collections[0].strips === 14, 'collections read from the summary');
assert(missing.visionAvailable === false && missing.visionNote.includes('no vision'), 'vision absence is kept as the server said it');

const ready = vi.statusFrom({ enabled: true, embedder: { dependencies_missing: [], model_downloaded: false }, vision: { available: true, model: 'qwen-vl' } });
assert(vi.readiness(ready).ready && vi.readiness(ready).reason === 'download', 'ready but the model is not downloaded yet');
assert(vi.readiness(vi.statusFrom({ enabled: false })).reason === 'off', 'switched off');
assert(vi.readiness(vi.statusFrom({})).ready === true, 'an empty status is not invented as an error');

const pdf = vi.hitFrom({ id: 'd~abc', source: '/a.pdf', kind: 'pdf', page: 3, y_offset: 1024, score: 0.5 });
const web = vi.hitFrom({ id: 'd~def', source: 'https://x.org', kind: 'web', y_offset: 0 });
const img = vi.hitFrom({ id: 'd~ghi', source: '/a.png', kind: 'image' });
assert(vi.hitLocation(pdf) === 'p. 3 · y 1024 px', 'pdf location');
assert(vi.hitLocation(web) === 'y 0 px', 'web location');
assert(vi.hitLocation(img) === '', 'single-strip picture has no location');
assert(vi.tileImageUrl('d~a b') === '/api/visual-index/tiles/d~a%20b/image', 'tile url is encoded');
assert(vi.shortSource('x'.repeat(100), 20).length === 20 && vi.shortSource('short') === 'short', 'long sources are shortened from the left');

if (failed) {
  console.error(`FAILED ${failed}`);
  process.exit(1);
}
console.log('ALL OK');
