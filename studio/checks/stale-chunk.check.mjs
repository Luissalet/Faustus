// 17-09 — the desktop window went blank on Connectors: yesterday's page
// asked for a chunk a rebuild had renamed. Every route chunk goes through
// lazyChunk (one reload on a stale chunk) and the routes sit inside an
// error boundary that keeps the shell on screen.
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
const root = fileURLToPath(new URL('../src/', import.meta.url));
function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.tsx?$/.test(name)) out.push(p);
  }
  return out;
}
const bare = [];
for (const file of walk(root)) {
  if (file.replace(/\\/g, '/').endsWith('shell/lazyChunk.tsx')) continue;
  const src = readFileSync(file, 'utf8');
  if (/\blazy\(/.test(src) || /React\.lazy\(/.test(src)) bare.push(file.slice(root.length).replace(/\\/g, '/'));
}
assert.deepEqual(bare, [], 'every lazy() must be lazyChunk(): ' + bare.join(', '));
const chunk = readFileSync(join(root, 'shell/lazyChunk.tsx'), 'utf8');
assert.match(chunk, /Failed to fetch dynamically imported module/, 'recognises the Chromium message');
assert.match(chunk, /sessionStorage\.setItem\(STAMP/, 'reloads at most once per minute');
assert.match(chunk, /window\.location\.reload\(\)/, 'reloads for the new index');
const shell = readFileSync(join(root, 'shell/AppShell.tsx'), 'utf8');
assert.match(shell, /<RouteErrorBoundary>\s*<Suspense/, 'routes are inside the error boundary');
console.log('stale-chunk: ALL OK');
