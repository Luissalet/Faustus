// The spreadsheet's pure logic (studio/src/lib/hojaModel.ts): addresses, the sparse row/column geometry the
// virtual grid scrolls over, clipboard text, the fill plan and the chart ticks.
// Bundled with esbuild on the fly; run by tests/test_surfaces_studio_js.py, or by hand:
//   node studio/checks/hoja.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-hoja-'));
const out = join(dir, 'model.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'lib', 'hojaModel.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const m = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

assert(m.colLetters(1) === 'A' && m.colLetters(26) === 'Z' && m.colLetters(27) === 'AA' && m.colLetters(702) === 'ZZ' && m.colLetters(703) === 'AAA', 'column letters');
assert(m.colNumber('A') === 1 && m.colNumber('AA') === 27 && m.colNumber('zz') === 702, 'column numbers');
assert(eq(m.parseAddr('$B$12'), { r: 12, c: 2 }) && m.parseAddr('12B') === null && m.addr(3, 28) === 'AB3', 'addresses');
assert(m.rangeText({ r0: 5, c0: 3, r1: 2, c1: 1 }) === 'A2:C5' && m.rangeText({ r0: 2, c0: 2, r1: 2, c1: 2 }) === 'B2', 'range text normalises the corners');
assert(m.inSel({ r0: 2, c0: 2, r1: 4, c1: 4 }, 3, 3) && !m.inSel({ r0: 2, c0: 2, r1: 4, c1: 4 }, 5, 3), 'selection contains');

{
  const ax = m.makeAxis(1000, 24, { 3: 48, 10: 10 }, [5, 6]);
  assert(ax.start(1) === 0 && ax.start(3) === 48 && ax.start(4) === 96, 'axis starts follow the overrides');
  assert(ax.size(5) === 0 && ax.start(7) === ax.start(5), 'hidden rows take no space');
  assert(ax.total === 1000 * 24 + (48 - 24) + (10 - 24) + (-24) * 2, 'axis total');
  let ok = true;
  for (const i of [1, 2, 3, 4, 9, 10, 11, 500, 1000]) { if (ax.at(ax.start(i) + (ax.size(i) > 0 ? ax.size(i) / 2 : 0)) !== i && ax.size(i) > 0) ok = false; }
  assert(ok, 'at() inverts start()');
  assert(ax.at(-5) === 1 && ax.at(1e9) === 1000, 'at() clamps');
  const big = m.makeAxis(1_048_576, 24, {});
  assert(big.total === 1_048_576 * 24 && big.at(24 * 99_999 + 3) === 100_000, 'a million rows need no table');
}
{
  const rows = [['a', 'b c'], ['x\ty', 'say "hi"'], ['', '1']];
  const text = m.toTsv(rows);
  assert(eq(m.parseTsv(text), rows), 'TSV survives tabs and quotes');
  assert(eq(m.parseTsv('1\t2\r\n3\t4\r\n'), [['1', '2'], ['3', '4']]), 'TSV reads Windows line ends');
  assert(m.toHtmlTable([['<a>']], [[{ b: 1 }]]).includes('&lt;a&gt;') && m.toHtmlTable([['x']], [[{ b: 1, bg: '#fff' }]]).includes('font-weight:bold'), 'HTML table escapes and styles');
}
{
  const v = (r, c) => ({ '1,1': 1, '2,1': 2, '1,2': 'x' }[`${r},${c}`]);
  const down = m.planFill({ r0: 1, c0: 1, r1: 2, c1: 1 }, { r0: 1, c0: 1, r1: 5, c1: 1 }, v);
  assert(eq(down, [{ range: 'A2:A5', from: 'A2', series: true, step: 1 }]), 'dragging a 1,2 pattern down continues it');
  const copy = m.planFill({ r0: 1, c0: 2, r1: 1, c1: 2 }, { r0: 1, c0: 2, r1: 4, c1: 2 }, v);
  assert(copy.length === 1 && copy[0].series === false && copy[0].range === 'B1:B4', 'a single cell is copied');
  const right = m.planFill({ r0: 1, c0: 1, r1: 1, c1: 1 }, { r0: 1, c0: 1, r1: 1, c1: 3 }, v);
  assert(right.length === 1 && right[0].range === 'A1:C1', 'dragging right fills along the row');
  assert(m.planFill({ r0: 1, c0: 1, r1: 2, c1: 2 }, { r0: 1, c0: 1, r1: 2, c1: 2 }, v).length === 0, 'no drag, no fill');
}
{
  assert(eq(m.summarise([1, 'x', 2, null, 3]), { count: 3, sum: 6, avg: 2 }), 'status bar numbers ignore text');
  const t = m.niceTicks(0, 97);
  assert(t[0] === 0 && t[t.length - 1] >= 97 && t.length >= 3 && t.length <= 12, 'nice ticks cover the data');
  assert(eq(m.niceTicks(5, 5).length > 1, true) && m.compactNumber(1234567) === '1.23M' && m.compactNumber(12.345) === '12.35', 'tick and number helpers');
}

if (failed) { console.error(`${failed} failed`); process.exit(1); }
console.log('ok hoja');
