// Composer ↑/↓ prompt history: the prompts sent from this browser, newest
// first, browsed only while the composer is empty or still shows the recalled
// entry (a real draft keeps the arrows for the caret). The pure helpers run
// for real (transpiled on the fly); the wiring is asserted on the source.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import ts from 'typescript';

const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8');
const js = ts.transpileModule(read('../src/screens/studio/promptHistory.ts'), { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText;
const h = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));

let list = [];
for (const line of ['uno', 'dos', 'dos', '  ', 'tres']) list = h.pushed(list, line);
assert.deepEqual(list, ['uno', 'dos', 'tres'], 'blank lines and consecutive repeats are folded');
let big = [];
for (let i = 0; i < h.MAX + 5; i++) big = h.pushed(big, `p${i}`);
assert.equal(big.length, h.MAX);
assert.equal(big[0], 'p5', 'the oldest go first');

assert.deepEqual(h.step(list, 3, -1), { index: 2, text: 'tres' }, '↑ from the fresh line: newest');
assert.deepEqual(h.step(list, 2, -1), { index: 1, text: 'dos' });
assert.deepEqual(h.step(list, 0, -1), { index: 0, text: 'uno' }, 'stops at the oldest');
assert.deepEqual(h.step(list, 2, 1), { index: 3, text: '' }, '↓ past the newest: back to an empty line');

assert.equal(h.browsing('', list, 3), true);
assert.equal(h.browsing('dos', list, 1), true, 'still showing the recalled entry');
assert.equal(h.browsing('dos y algo más', list, 1), false, 'an edited draft keeps the arrows');

const composer = read('../src/screens/studio/Composer.tsx');
assert.match(composer, /from '\.\/promptHistory'/);
assert.match(composer, /remember\(current\)/, 'every send is remembered');
assert.match(composer, /browsing\(draft, h\.list, h\.index\)/, 'arrows browse only an empty or recalled composer');
console.log('prompt-history: ALL OK');
