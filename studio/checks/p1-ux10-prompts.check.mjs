// UX-10: the prompt library's pure client-side render preview
// (studio/src/adapters/prompts.ts::previewRender) — the live preview shown
// in the "Use" dialog before the round trip to POST /api/prompts/{id}/render
// that actually validates required variables.
//
// Run by tests/test_p1_ux_10_js.py, or by hand:
//   node studio/checks/p1-ux10-prompts.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-prompts-')), 'prompts.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'adapters', 'prompts.ts')],
  bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent',
});
const { previewRender } = await import(pathToFileURL(out).href);

assert.equal(previewRender('Fix {{issue}} in {{area}}', { issue: 'login crash', area: 'backend' }), 'Fix login crash in backend');
// A variable with no value yet stays visible as the token, not blank text —
// the sender must see what is still unfilled before rendering server-side.
assert.equal(previewRender('Fix {{issue}} in {{area}}', { issue: 'login crash' }), 'Fix login crash in {{area}}');
assert.equal(previewRender('No variables here', {}), 'No variables here');

console.log('p1-ux10-prompts.check.mjs OK');
