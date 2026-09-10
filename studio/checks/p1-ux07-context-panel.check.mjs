// UX-07: which `@file` sources a draft mentions, and that a removed mention
// drops its stale exclude/pin instead of silently keeping it applied
// (studio/src/screens/studio/ContextPanel.tsx).
//
// Run by tests/test_p1_ux_07_js.py, or by hand:
//   node studio/checks/p1-ux07-context-panel.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-context-panel-')), 'ContextPanel.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'screens', 'studio', 'ContextPanel.tsx')],
  bundle: true, platform: 'node', format: 'esm', jsx: 'automatic',
  outfile: out, logLevel: 'silent',
});
const { mentionedSources, pruneOverrides } = await import(pathToFileURL(out).href);

assert.deepEqual(mentionedSources('Look at @src/app.py and @docs/spec.md, please'), ['src/app.py', 'docs/spec.md']);
assert.deepEqual(mentionedSources('no mentions here'), []);
// Repeated mentions of the same path only count once.
assert.deepEqual(mentionedSources('@a.py then @a.py again'), ['a.py']);

const overrides = { excludeSources: ['gone.py', 'still-here.py'], pinSources: ['still-here.py'] };
const pruned = pruneOverrides(overrides, 'only @still-here.py is mentioned now');
assert.deepEqual(pruned.excludeSources, ['still-here.py']);
assert.deepEqual(pruned.pinSources, ['still-here.py']);

console.log('p1-ux07-context-panel.check.mjs OK');
