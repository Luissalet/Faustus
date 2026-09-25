// Studio: a cited tool-result id (`[L-000011]`) becomes a footnote saying
// what that result was, instead of raw noise in the reply.
//
// Run by tests/test_cite_tool_results_js.py, or by hand:
//   node studio/checks/cite-tool-results.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-cite-')), 'transcript.mjs');
await build({
  entryPoints: [join(root, 'studio/src/screens/studio/Transcript.tsx')], bundle: true,
  platform: 'node', format: 'esm', jsx: 'automatic', outfile: out, logLevel: 'silent',
});
globalThis.window ??= { localStorage: { getItem: () => null, setItem: () => {} } };
globalThis.document ??= { documentElement: { toggleAttribute: () => {} } };
const { citeToolResults } = await import(pathToFileURL(out).href);

const calc = (id, input, decimal) => ({
  id: id, tool: 'mcp__26a426d3__calc', label: 'calc', state: 'done', round: 1,
  output: JSON.stringify({ id, cite: `[${id}]`, input, exact: 'x', decimal }),
});
const steps = [calc('L-000011', 'pct(15, 86.40)', '12.96'), calc('L-000012', '(86.40 + pct(15, 86.40)) / 4', '24.84')];

const text = 'La propina es **12,96 €** [L-000011]. Cada uno paga **24,84 €** [L-000012], y otra vez [L-000011].';
const got = citeToolResults(text, steps);
assert.match(got, /\*\*12,96 €\*\* \[\^L-000011\]\./);
assert.match(got, /\[\^L-000012\]/);
assert.equal((got.match(/^\[\^L-000011\]: /gm) || []).length, 1, 'one note per id');
assert.match(got, /^\[\^L-000011\]: pct\(15, 86\.40\) = 12\.96 · calc \(L-000011\)$/m);
assert.match(got, /^\[\^L-000012\]: \(86\.40 \+ pct\(15, 86\.40\)\) \/ 4 = 24\.84 · calc \(L-000012\)$/m);

// An id no step returned stays as written; no steps, no change.
assert.equal(citeToolResults('Visto en [L-000099].', steps), 'Visto en [L-000099].');
assert.equal(citeToolResults(text, []), text);
// A markdown link whose text looks like an id is left alone.
assert.equal(citeToolResults('[L-000011](https://example.com)', steps), '[L-000011](https://example.com)');
// Non-JSON output: the start of the text.
const plain = [{ id: 's', tool: 'mcp__8e9ef00b__analysis', label: 'mcp 8e9ef00b analysis · {"q": 1}', state: 'done', round: 1, output: 'N-000123 mean 4.2, sd 1.1' }];
assert.match(citeToolResults('media 4,2 [N-000123]', plain), /^\[\^N-000123\]: N-000123 mean 4\.2, sd 1\.1 · analysis \(N-000123\)$/m);

console.log('cite-tool-results: ok');
