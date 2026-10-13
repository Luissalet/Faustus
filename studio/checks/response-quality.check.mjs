import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { Window } from 'happy-dom';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const sources = Array.from({ length: 9 }, (_, i) => ({ title: `Source ${i + 1}`, url: `https://example.com/article/${i + 1}?version=1`, favicon: '/api/favicon?domain=example.com' }));
const text = 'Conclusion with [specific evidence](https://example.com/article/1?version=1#data).\n\nAn [unobserved version](https://example.com/article/1?version=2).\n\n'
  + '```entity\n' + JSON.stringify({ title: 'Subject title', url: sources[0].url, image: 'data:image/png;base64,aGVsbG8=', body: '**Observed**: 12. [Source 2](' + sources[1].url + ')\n\nText[^a]\n\n[^a]: Retained footnote.' }) + '\n```\n\n'
  + '```details\n' + JSON.stringify({ summary: 'Method and limitations', body: 'Only two observations. <script>bad()</script>\n\n[Data](' + sources[2].url + ')' }) + '\n```\n\n'
  + '```entity\n{"title":"unfinished"}\n```\n\n`​https://example.com/article/1?version=1`';
const harness = `import React from 'react'; import {createRoot} from 'react-dom/client'; import {flushSync} from 'react-dom';
import {Rich} from './studio/src/screens/rich'; import {ResponseSources} from './studio/src/components/ResponseSources';
export {parsePresentation,findResponseSource} from './studio/src/lib/responsePresentation';
export function mount(text,sources){const r=createRoot(document.getElementById('root'));flushSync(()=>r.render(<><Rich text={text} sources={sources}/><ResponseSources sources={sources}/></>));return r;}`;
const bundled = await build({ stdin: { contents: harness, resolveDir: root, loader: 'tsx' }, bundle: true, platform: 'browser', format: 'esm', write: false, loader: { '.css': 'empty' }, define: { 'process.env.NODE_ENV': '"production"' }, logLevel: 'silent' });
const dir = mkdtempSync(join(tmpdir(), 'response-quality-'));
writeFileSync(join(dir, 'check.mjs'), bundled.outputFiles[0].text);
const win = new Window({ url: 'http://127.0.0.1:63348' });
for (const key of ['window','document','HTMLElement','SVGElement','Node','Element','Event','CustomEvent','MouseEvent','localStorage','sessionStorage','MutationObserver','getComputedStyle','requestAnimationFrame','cancelAnimationFrame']) {
  if (key in win) globalThis[key] = win[key];
}
document.body.innerHTML = '<div id="root"></div>';
const mod = await import(pathToFileURL(join(dir, 'check.mjs')).href);
const mounted = mod.mount(text, [...sources, {url:'/clev?tracking=1', title:'Search navigation'}]);
assert.equal(document.querySelectorAll('.fs-response-sources__list li').length, 9, 'all observed sources survive');
assert.equal(document.querySelector('.fs-response-sources').open, false, 'source list is a disclosure');
assert.match(document.querySelector('.fs-response-sources summary').textContent, /9/);
const first = document.querySelector('.fs-rich > p a');
assert.equal(first.className, 'fs-response-source', 'exact observed citation is a chip');
assert.match(first.href, /version=1#data$/, 'preserve specific URL and fragment');
assert.match(first.textContent, /specific evidence/, 'original label retained');
assert.equal(document.querySelectorAll('.fs-rich > p')[1].querySelector('a').className, 'fs-link', 'other query is not identified as observed');
assert.equal(document.querySelector('.fs-response-entity h3').textContent, 'Subject title', 'entity title retained');
assert.equal(document.querySelector('.fs-response-entity img').alt, 'Subject title');
assert.match(document.querySelector('.fs-rich__notes').textContent, /Retained footnote/);
assert.equal(document.querySelector('.fs-response-details').open, false);
assert.match(document.querySelector('.fs-response-details').textContent, /Only two observations/);
assert.equal(document.querySelectorAll('script').length, 0, 'body is Markdown text, not executable HTML');
assert.match(document.querySelector('.fs-rich__code').textContent, /unfinished/, 'invalid block is retained as code');
assert.equal(document.querySelector('code').querySelector('a'), null, 'code does not turn into citations');
assert.equal(mod.findResponseSource('https://example.com/article/99', sources), undefined);
assert.equal(mod.findResponseSource('javascript:alert(1)', sources), undefined);
assert.equal(mod.parsePresentation('entity', '{"title":"X","body":"Y","image":3}'), null);
mounted.unmount();
mod.mount('[plain link](https://example.com/article/1?version=1)', []);
assert.equal(document.querySelector('.fs-rich a').className, 'fs-link', 'legacy/unsourced reply remains ordinary Markdown');
console.log('response-quality: ALL OK (17 behavior checks)');
win.happyDOM.abort();
