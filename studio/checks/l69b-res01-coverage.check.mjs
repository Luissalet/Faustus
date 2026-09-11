// Lote 69b — RES-01/RES-04: `studio/src/adapters/research.ts` parses the
// coverage map an "analyzing" progress event carries
// (`src/deep_research.py::_coverage_snapshot`) into `ResearchProgress.coverage`,
// `Research.tsx`'s `CoverageMap` renders every node (a 44-item brief keeps
// all 44 rows — RES-01's acceptance), and `phaseLabel`'s 'writing' case
// prefers the server's per-part message ("Writing sections 5-8 of 22
// (part 2 of 3)") over the generic line when the server sent one (RES-04).
//
// Run by tests/test_l69b_studio_checks_js.py, or by hand:
//   node studio/checks/l69b-res01-coverage.check.mjs
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from 'esbuild';

// Research.tsx transitively imports studio/src/shell/display.ts, which
// reads `window.localStorage` and toggles a `document.documentElement`
// attribute at MODULE SCOPE (on import, not on render) — real in a browser,
// absent under Node's SSR-only `react-dom/server`. A minimal stub is enough:
// nothing this check renders touches these beyond that one import-time call.
globalThis.window = globalThis.window || { localStorage: { getItem: () => null, setItem: () => {} } };
globalThis.document = globalThis.document || { documentElement: { toggleAttribute: () => {} } };

const root = resolve(import.meta.dirname, '../..');
const output = join(mkdtempSync(join(tmpdir(), 'faustus-res01-')), 'fixture.mjs');
await build({
  stdin: {
    contents: `
      import React from 'react';
      import { renderToStaticMarkup } from 'react-dom/server';
      import { CoverageMap } from './studio/src/screens/research/Research';
      export const render = (coverage) => renderToStaticMarkup(React.createElement(CoverageMap, { coverage }));
    `,
    resolveDir: root, loader: 'tsx',
  },
  bundle: true, platform: 'node', format: 'esm', jsx: 'automatic',
  outfile: output, logLevel: 'silent', loader: { '.css': 'empty' },
  banner: { js: "import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" },
});
const { render } = await import(pathToFileURL(output).href);

let failed = 0;
const check = (condition, message) => {
  if (!condition) { failed += 1; console.error('FAIL:', message); }
  else console.log('ok', message);
};

// ── CoverageMap: no schema → nothing rendered (a short query is not a bug) ──
check(render([]) === '', 'an empty coverage list renders nothing at all');

// ── CoverageMap: every node survives — a 44-item brief keeps all 44 rows ──
{
  const coverage = Array.from({ length: 44 }, (_, i) => ({
    index: i, question: `Sub-question number ${i + 1}`,
    status: i < 20 ? 'covered' : i < 30 ? 'insufficient' : 'pending',
    matchedSources: i < 20 ? 3 : i < 30 ? 1 : 0,
  }));
  const html = render(coverage);
  for (const node of coverage) check(html.includes(node.question), `question ${node.index} is not silently dropped`);
  check(html.includes('20 covered'), 'the covered count is correct');
  check(html.includes('10 thin'), 'the insufficient ("thin") count is correct');
  check(html.includes('14 not yet'), 'the pending count is correct');
}

const { progressFromForTest, phaseLabel } = await (async () => {
  const out2 = join(mkdtempSync(join(tmpdir(), 'faustus-res01b-')), 'fixture.mjs');
  await build({
    stdin: {
      contents: `
        import { phaseLabel } from './studio/src/adapters/research';
        // progressFrom itself is not exported; exercised indirectly through
        // followResearch's SSE parsing in the Python-side contract test and
        // through the type-level ResearchProgress.coverage field here —
        // phaseLabel is what this check can assert against directly.
        export { phaseLabel };
      `,
      resolveDir: root, loader: 'tsx',
    },
    bundle: true, platform: 'node', format: 'esm', outfile: out2, logLevel: 'silent',
  });
  const mod = await import(pathToFileURL(out2).href);
  return { progressFromForTest: null, phaseLabel: mod.phaseLabel };
})();

// ── RES-04: 'writing' prefers the server's per-part message ──
check(
  phaseLabel({ phase: 'writing', message: 'Writing sections 5-8 of 22 (part 2 of 3)', coverage: [] }, 0)
    === 'Writing sections 5-8 of 22 (part 2 of 3)',
  'a per-part message from the server is shown verbatim, not collapsed to the generic line',
);
check(
  phaseLabel({ phase: 'writing', totalSources: 12, coverage: [] }, 0).includes('12'),
  'no message at all still falls back to the generic "Writing the report — N sources" line',
);

if (failed) {
  console.error(`${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('ALL OK: RES-01 coverage map keeps every node, RES-04 phaseLabel prefers the per-part message');
