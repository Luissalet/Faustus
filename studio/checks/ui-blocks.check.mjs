// Reply blocks (lib/uiBlocks.ts): the validator, plus the renderer wiring in
// rich.tsx and Studio.tsx. Run by hand:  node studio/checks/ui-blocks.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-uib-'));
const out = join(dir, 'uiBlocks.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'lib', 'uiBlocks.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const ub = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };

{
  const r = ub.parseUiBlock('choices', JSON.stringify({ question: 'Q?', options: ['A', { label: 'B', detail: 'b' }, 'a'] }));
  assert(r.ok && r.spec.kind === 'choices', 'choices parses');
  assert(r.ok && r.spec.options.length === 2, 'duplicate option (case-insensitive) dropped');
  assert(r.ok && r.spec.options[1].detail === 'b', 'detail kept');
  assert(r.ok && r.spec.multi === false, 'single choice by default');
}
{
  const r = ub.parseUiBlock('faustus-choices', JSON.stringify(['x', 'y', 'z']));
  assert(r.ok && r.spec.options.length === 3, 'a bare array is a list of options');
}
{
  const many = Array.from({ length: 20 }, (_, i) => `o${i}`);
  const r = ub.parseUiBlock('choices', JSON.stringify({ options: many, multi: true }));
  assert(r.ok && r.spec.options.length === ub.UI_MAX_OPTIONS && r.spec.multi, 'options capped, multi kept');
}
assert(!ub.parseUiBlock('choices', JSON.stringify({ options: ['only'] })).ok, 'one option is not a choice');
assert(!ub.parseUiBlock('choices', '{nope').ok, 'bad JSON rejected');
{
  const r = ub.parseUiBlock('decision', JSON.stringify({ title: 'T', options: [
    { name: 'A', pros: ['p1'], cons: 'c1', recommended: true },
    { name: 'B', recommended: true },
  ], verdict: 'A' }));
  assert(r.ok && r.spec.kind === 'decision', 'decision parses');
  assert(r.ok && r.spec.options[0].cons.length === 1, 'a single con string becomes a list');
  assert(r.ok && r.spec.options.filter((o) => o.recommended).length === 1, 'one recommendation at most');
}
assert(!ub.parseUiBlock('decision', JSON.stringify({ options: [{ name: 'A' }, { nope: 1 }] })).ok, 'decision needs two named options');
{
  const long = 'x'.repeat(500);
  const r = ub.parseUiBlock('choices', JSON.stringify({ options: [long, 'b'] }));
  assert(r.ok && r.spec.options[0].label.length === ub.UI_MAX_STRING, 'strings capped');
}
assert(ub.parseUiBlock('choices', JSON.stringify({ kind: 'decision', options: [{ name: 'A' }, { name: 'B' }] })).ok, 'kind in JSON wins over the fence');

const rich = readFileSync(join(root, 'studio', 'src', 'screens', 'rich.tsx'), 'utf8');
assert(rich.includes('UI_BLOCK_LANGS.has(') && rich.includes('<UiFence'), 'rich.tsx routes the fences to UiFence');
assert(rich.includes('export const RichReply'), 'rich.tsx exports the reply context');
const studio = readFileSync(join(root, 'studio', 'src', 'screens', 'Studio.tsx'), 'utf8');
assert(studio.includes('<RichReply.Provider value={replyFromBlock}>'), 'Studio provides the composer to the transcript');
const comp = readFileSync(join(root, 'studio', 'src', 'components', 'UiBlock.tsx'), 'utf8');
assert(comp.includes('disabled={!onReply}'), 'without a composer the buttons are disabled');

if (failed) { console.error(`${failed} failed`); process.exit(1); }
console.log('all ui-blocks checks passed');
