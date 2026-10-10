import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { Window } from 'happy-dom';
const browser = new Window({ url: 'http://127.0.0.1/' });
globalThis.window = browser;
globalThis.document = browser.document;
globalThis.localStorage = browser.localStorage;
Object.defineProperty(globalThis, 'navigator', { value: browser.navigator, configurable: true });
const root = resolve(process.argv[2] || '.');
const out = join(mkdtempSync(join(tmpdir(), 'faustus-advisory-render-')), 'render.cjs');
await build({ stdin: { resolveDir: root, contents: `
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { CheckList } from './studio/src/screens/studio/Harness';
export const render = (checks) => renderToStaticMarkup(createElement(CheckList, { checks }));
` }, bundle: true, platform: 'node', format: 'cjs', outfile: out,
loader: { '.css': 'empty' }, logLevel: 'silent' });
const { render } = createRequire(import.meta.url)(out);
const detail = 'Pending plan (0/3). Work is preserved; no additional approval is required.';
const html = render([{ status: 'advisory', round: 4,
  reasons: ['runtime_stop:plan_no_progress'], detail }]);
assert.ok(html.includes(detail), 'Human-readable persisted notice must remain visible alongside reasons');
assert.ok(html.includes('runtime_stop:plan_no_progress'), 'Existing reasons are preserved');
assert.ok(render([{ status: 'tests_failed', reasons: ['syntax_error'], detail: 'Actual syntax failure' }])
  .includes('Actual syntax failure'));
assert.ok(render([{ status: 'verified' }]).includes('Checks completed'));
assert.equal(render([]), '');
console.log('PASS: real CheckList keeps readable advisory details and existing reasons');
