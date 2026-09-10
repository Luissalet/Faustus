// BENCH-06 - the sandbox/CSP policy behind the workbench's artifact preview
// (studio/src/components/previewSandbox.ts / Preview.tsx). Pure functions,
// no React needed to check them.
//
// Bundled with esbuild on the fly; run by tests/test_p1_bench06_preview_js.py,
// or by hand:
//   node studio/checks/p1-bench06-preview.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-preview-'));
async function load(rel, name) {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', rel)], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const p = await load(join('components', 'previewSandbox.ts'), 'previewSandbox.mjs');

let failed = 0;
const assert = (c, msg) => {
  if (!c) { failed += 1; console.error('FAIL:', msg); }
  else console.log('ok:', msg);
};

// ── sandbox tokens ──
{
  const off = p.sandboxAttr(false);
  const on = p.sandboxAttr(true);
  assert(!off.includes('allow-scripts'), 'scripts are off by default');
  assert(on.includes('allow-scripts'), 'allowScripts turns scripts on, explicitly');
  assert(!off.includes('allow-same-origin') && !on.includes('allow-same-origin'), 'allow-same-origin is NEVER granted, opt-in or not');
  assert(off.includes('allow-popups') && on.includes('allow-popups'), 'popups are allowed either way');
}

// ── CSP: no network, scripts opt-in only ──
{
  const cspOff = p.previewCsp(false);
  const cspOn = p.previewCsp(true);
  assert(cspOff.includes("script-src 'none'"), 'no scripts run without opting in');
  assert(cspOn.includes("script-src 'unsafe-inline'") && !cspOn.includes("script-src 'none'"), 'opting in enables script-src, not blanket unsafe-eval');
  assert(!cspOn.includes('unsafe-eval'), 'never unsafe-eval, even opted in — matches SEC-07/QA-32 desktop CSPs');
  for (const csp of [cspOff, cspOn]) {
    assert(csp.includes("connect-src 'none'"), 'no outbound network from either policy');
    assert(csp.includes("frame-src 'none'"), 'no nested frames either');
    assert(csp.includes("default-src 'none'"), 'default-src none — everything else is an explicit allow');
  }
}

// ── the wrapped document actually carries the CSP as the first head tag ──
{
  const doc = p.wrapPreviewHtml('<b>hi</b><script>evil()</script>', false);
  assert(doc.includes('<meta http-equiv="Content-Security-Policy"'), 'CSP meta tag present');
  assert(doc.indexOf('Content-Security-Policy') < doc.indexOf('<body>'), 'CSP declared before the body renders');
  assert(doc.includes('<b>hi</b>'), 'the artifact body is preserved verbatim');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
