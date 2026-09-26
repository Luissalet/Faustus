// Style check (lib/proseLint.ts) plus its wiring in the document editor.
// Run by hand:  node studio/checks/prose-lint.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-prose-'));
const out = join(dir, 'proseLint.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'lib', 'proseLint.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const pl = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };
const rules = (text) => pl.lintProse(text).map((i) => i.rule);

assert(rules('Cabe destacar que el plan funciona.').includes('stock-phrase'), 'Spanish stock phrase');
assert(rules("It's worth noting that we delve into it.").filter((r) => r === 'stock-phrase').length === 2, 'English stock phrases');
assert(rules('No solo es rápido, sino también barato.').includes('not-only-but'), 'no solo… sino');
assert(rules('This is not only fast but cheap.').includes('not-only-but'), 'not only… but');
assert(rules("It's not a bug, it's a feature.").includes('not-x-its-y'), "it's not X, it's Y");
assert(rules('No es un fallo, es una decisión.').includes('not-x-its-y'), 'no es X, es Y');
assert(rules('¿El resultado? Todo funciona.').includes('rhetorical-question'), 'rhetorical question ES');
assert(rules('The catch? It costs more.').includes('rhetorical-question'), 'rhetorical question EN');
assert(rules('¡Claro! Aquí tienes.').includes('opener'), 'chatty opener');
assert(rules('Esto podría potencialmente fallar.').includes('hedge-stack'), 'stacked hedge');
assert(rules('Uno — dos — tres — cuatro.').includes('dash-heavy'), 'dash-heavy paragraph');
assert(rules('Un texto normal y claro, sin nada raro.').length === 0, 'clean text has no issues');
assert(rules('```\ncabe destacar\n```\n').length === 0, 'code fences are skipped');
assert(rules('El sololaje no es nada.').length === 0, 'no false match inside words');
{
  const text = 'Hola.\n\nHoy en día todo va rápido.';
  const [i] = pl.lintProse(text);
  assert(i && text.slice(i.start, i.end).toLowerCase() === 'hoy en día', 'offsets point at the phrase');
}
{
  const s = pl.proseSummary(pl.lintProse('Cabe destacar esto. Hoy en día, cabe señalar aquello.'));
  assert(s[0].label === 'Stock phrase' && s[0].count === 3, 'summary counts per label');
}
const editor = readFileSync(join(root, 'studio', 'src', 'screens', 'documents', 'Editor.tsx'), 'utf8');
assert(editor.includes('testId="doc-style-check"') && editor.includes('data-testid="doc-style-panel"'), 'editor has the button and the panel');
assert(!/setText\([^)]*issue/.test(editor), 'the check never edits the text');

if (failed) { console.error(`${failed} failed`); process.exit(1); }
console.log('all prose-lint checks passed');
