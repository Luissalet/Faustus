// The chart-fence validator (lib/chartSpec.ts) — pure, no React, no browser.
// Bundled with esbuild on the fly; run by tests/test_studio_chart_spec_js.py,
// or by hand:
//   node studio/checks/chartSpec.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-chart-'));

async function load(rel, name) {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', 'lib', rel)], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const cs = await load('chartSpec.ts', 'chartSpec.mjs');

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

const ok = (raw) => cs.parseChartSpec(raw);

// ── Valid specs ──
{
  const r = ok(JSON.stringify({ type: 'bar', x: ['a', 'b'], series: [{ name: 's1', values: [1, 2] }] }));
  assert(r.ok === true, 'valid bar parses');
  assert(r.ok && r.spec.type === 'bar', 'bar type preserved');
}
{
  const r = ok(JSON.stringify({ type: 'line', series: [{ name: 's1', values: [1, 2, 3] }, { name: 's2', values: [4, 5, 6] }] }));
  assert(r.ok === true, 'valid line with two series parses');
}
{
  const r = ok(JSON.stringify({ type: 'pie', x: ['a', 'b', 'c'], series: [{ name: 's1', values: [1, 2, 3] }] }));
  assert(r.ok === true, 'valid pie parses');
}
{
  const r = ok(JSON.stringify({ type: 'area', stacked: true, series: [{ name: 's1', values: [1, 2] }, { name: 's2', values: [3, 4] }] }));
  assert(r.ok === true, 'valid stacked area parses');
}

// ── Bad JSON ──
{
  const r = ok('{not json');
  assert(r.ok === false, 'invalid JSON rejected');
}
{
  const r = ok(JSON.stringify([1, 2, 3]));
  assert(r.ok === false, 'an array (not object) is rejected');
}

// ── Structural errors ──
{
  const r = ok(JSON.stringify({ type: 'bogus', series: [{ name: 's', values: [1] }] }));
  assert(r.ok === false, 'unknown type rejected');
}
{
  const r = ok(JSON.stringify({ type: 'bar', series: [] }));
  assert(r.ok === false, 'empty series array rejected');
}
{
  const r = ok(JSON.stringify({ type: 'bar', series: [{ name: 's', values: [1, 2] }, { name: 't', values: [1] }] }));
  assert(r.ok === false, 'ragged series (different lengths) rejected');
}
{
  const r = ok(JSON.stringify({ type: 'bar', x: ['a'], series: [{ name: 's', values: [1, 2] }] }));
  assert(r.ok === false, 'x length mismatch rejected');
}

// ── NaN / non-numeric / negative pie ──
{
  const r = ok('{"type":"bar","series":[{"name":"s","values":[1,NaN]}]}');
  assert(r.ok === false, 'NaN in values rejected (also invalid JSON since JSON has no NaN literal)');
}
{
  const r = ok(JSON.stringify({ type: 'bar', series: [{ name: 's', values: ['1', 2] }] }));
  assert(r.ok === false, 'string in values rejected');
}
{
  const r = ok(JSON.stringify({ type: 'pie', series: [{ name: 's', values: [1, -2] }] }));
  assert(r.ok === false, 'negative pie value rejected');
}
{
  // JSON.stringify turns Infinity into `null`; the validator must reject the
  // resulting null value rather than silently coercing it to 0.
  const r = ok(JSON.stringify({ type: 'bar', series: [{ name: 's', values: [Number.POSITIVE_INFINITY] }] }));
  assert(r.ok === false, 'a non-finite value (serialized as null) is rejected');
}

// ── Caps ──
{
  const manySeries = Array.from({ length: 13 }, (_, i) => ({ name: `s${i}`, values: [1] }));
  const r = ok(JSON.stringify({ type: 'bar', series: manySeries }));
  assert(r.ok === false, 'more than 12 series rejected');
}
{
  const manyPoints = Array.from({ length: 201 }, () => 1);
  const r = ok(JSON.stringify({ type: 'line', series: [{ name: 's', values: manyPoints }] }));
  assert(r.ok === false, 'more than 200 points rejected');
}
{
  const manySlices = Array.from({ length: 41 }, () => 1);
  const r = ok(JSON.stringify({ type: 'pie', series: [{ name: 's', values: manySlices }] }));
  assert(r.ok === false, 'more than 40 pie slices rejected');
}
{
  const longTitle = 'x'.repeat(200);
  const r = ok(JSON.stringify({ type: 'bar', title: longTitle, series: [{ name: 's', values: [1] }] }));
  assert(r.ok === true && r.spec.title.length === 60, 'an over-long title is clipped to 60 chars, not rejected');
}

// ── stacked only on bar/area ──
{
  const r = ok(JSON.stringify({ type: 'line', stacked: true, series: [{ name: 's', values: [1] }] }));
  assert(r.ok === false, 'stacked on a line chart is rejected');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
