// Actual React screen + adapter, deterministic fetch only; no Git/backend.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { Window } from 'happy-dom';
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const dom = new Window({ url: 'http://localhost' });
for (const key of ['window', 'document', 'localStorage', 'navigator', 'HTMLElement'])
  Object.defineProperty(globalThis, key, { value: dom[key] ?? dom, configurable: true });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
process.env.NODE_ENV = 'development';   // act() exists only in React's development build
localStorage.setItem('faustus_studio_lang', 'en');
const source = JSON.parse(readFileSync('plugins/marketplace.json', 'utf8'));
const makeCatalogue = () => ({ root: 'D:/Fixture/plugins', plugins: source.plugins.map(row => {
  const manifest = JSON.parse(readFileSync(`plugins/${row.id}/plugin.json`, 'utf8'));
  return { ...row, name: manifest.name, purpose: manifest.purpose, source_kind: null, local_path: null,
    clone_path: `D:/Fixture/plugins/${row.id}`, state: 'not_installed', can_install: true };
}) });
const expectedCount = source.plugins.length;
assert.ok(expectedCount >= 26);
assert.equal(new Set(source.plugins.map(row => row.id)).size, expectedCount);
const result = await build({ stdin: { contents: "export {PluginMarketplace} from './studio/src/screens/connectors/PluginMarketplace'; export {setLang} from './studio/src/i18n';", resolveDir: process.cwd(), loader: 'tsx' },
  bundle: true, jsx: 'automatic', format: 'esm', platform: 'node', packages: 'external',
  write: false, loader: { '.css': 'empty' } });
const output = resolve('.plugin-marketplace-check.mjs');
const previewOutput = resolve('.plugin-marketplace-preview-check.mjs');
writeFileSync(output, result.outputFiles[0].text);
try {
  const React = await import('react'); const { act } = React;
  const { createRoot } = await import('react-dom/client');
  const { PluginMarketplace, setLang } = await import(pathToFileURL(output));
  const host = document.createElement('div'); document.body.append(host); const root = createRoot(host);
  let catalogue = makeCatalogue(), calls = [], failPost = false, failGet = false, releaseInstall;
  const configured = [];
  globalThis.fetch = async (input, options = {}) => {
    const url = String(input), method = options.method ?? 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ url, method, body });
    if (method === 'GET') return Response.json(failGet ? { detail: 'Synthetic list unavailable' } : catalogue,
      { status: failGet ? 502 : 200 });
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers['Content-Type'], 'application/json');
    if (failPost) return Response.json({ detail: 'Synthetic download refused' }, { status: 503 });
    const [, id, action] = url.match(/\/api\/plugin-marketplace\/([^/]+)\/(install|link|unlink)$/) ?? [];
    assert.ok(id && action, `unexpected fixture request ${url}`);
    const row = catalogue.plugins.find(p => p.id === decodeURIComponent(id));
    if (action === 'install') return new Promise(resolve => { releaseInstall = () => {
      Object.assign(row, { state: 'cloned', source_kind: 'cloned', local_path: row.clone_path, can_install: false });
      resolve(Response.json({ state: 'cloned', path: row.clone_path }));
    }; });
    if (action === 'link') Object.assign(row, { state: 'linked', source_kind: 'linked', local_path: body.path, can_install: false });
    if (action === 'unlink') Object.assign(row, { state: 'not_installed', source_kind: null, local_path: null, can_install: true });
    return Response.json({ files_preserved: true });
  };
  const flush = async fn => act(async () => { await fn?.(); await new Promise(resolve => setTimeout(resolve, 0)); });
  const row = id => host.querySelector(`[data-testid="marketplace-${id}"]`);
  const button = (container, label) => [...container.querySelectorAll('button')].find(b => b.textContent.trim() === label);
  const edit = (input, value) => {
    Object.getOwnPropertyDescriptor(dom.HTMLInputElement.prototype, 'value').set.call(input, value);
    input.dispatchEvent(new dom.Event('input', { bubbles: true }));
  };
  const count = () => host.querySelectorAll('.fs-conn__list > li').length;
  await flush(() => root.render(React.createElement(PluginMarketplace, { onConfigure: (...args) => configured.push(args) })));
  assert.equal(count(), expectedCount); assert.ok(row('hoardhub').textContent.includes('Required'));
  await flush(() => edit(host.querySelector('input[type="search"]'), 'not-a-real-plugin'));
  assert.equal(count(), 1); assert.ok(row('hoardhub'));
  await flush(() => edit(host.querySelector('input[type="search"]'), ''));
  assert.equal(count(), expectedCount);
  await flush(() => button(row('argus'), 'Download').click());
  assert.equal(calls.filter(c => c.method === 'POST').length, 1);
  assert.equal(calls.at(-1).url, '/api/plugin-marketplace/argus/install');
  assert.deepEqual(calls.at(-1).body, {});
  assert.ok([...host.querySelectorAll('button')].filter(b => b.textContent.includes('Download')).every(b => b.disabled));
  await flush(() => releaseInstall());
  assert.ok(row('argus').textContent.includes('Source downloaded'));
  assert.ok(row('argus').textContent.includes('D:/Fixture/plugins/argus'));
  assert.equal(calls.filter(c => c.method === 'GET').length, 2);
  await flush(() => button(row('argus'), 'Configure connector').click());
  assert.deepEqual(configured, [['argus', 'D:/Fixture/plugins/argus']]);
  await flush(() => button(row('babel'), 'Link existing folder').click());
  const path = host.querySelector('#plugin-path-babel'); assert.ok(path);
  await flush(() => edit(path, '  D:/Fixture/existing Hoards/Babel  '));
  await flush(() => row('babel').querySelector('form').dispatchEvent(new dom.Event('submit', { bubbles: true, cancelable: true })));
  assert.deepEqual(calls.find(c => c.url.endsWith('/babel/link')).body, { path: 'D:/Fixture/existing Hoards/Babel' });
  assert.ok(row('babel').textContent.includes('Linked folder'));
  assert.ok(row('babel').textContent.includes('D:/Fixture/existing Hoards/Babel'));
  await flush(() => button(row('babel'), 'Forget link').click());
  assert.equal(calls.filter(c => c.url.endsWith('/babel/unlink')).length, 1);
  assert.deepEqual(calls.find(c => c.url.endsWith('/babel/unlink')).body, {});
  assert.ok(host.querySelector('[role="status"]').textContent.includes('files kept'));
  assert.equal(row('babel').querySelector('.fs-marketplace__path'), null);
  assert.ok(button(row('babel'), 'Download'));
  failPost = true;
  await flush(() => button(row('cassandra'), 'Download').click());
  assert.ok(host.querySelector('[role="alert"]').textContent.includes('Synthetic download refused'));
  assert.equal(count(), expectedCount); assert.ok(row('argus').textContent.includes('D:/Fixture/plugins/argus'));
  failGet = true;
  await flush(() => button(host, 'Refresh').click());
  assert.ok(host.querySelector('[role="alert"]').textContent.includes('Synthetic list unavailable'));
  assert.equal(count(), expectedCount);
  assert.ok(row('argus').textContent.includes('D:/Fixture/plugins/argus'));
  assert.ok([...host.querySelectorAll('a')].every(a => a.rel === 'noopener noreferrer' && a.target === '_blank'));
  await flush(() => root.render(React.createElement(PluginMarketplace, { key: 'initial-failure' })));
  assert.ok(button(host, 'Try again')); assert.equal(count(), 0);
  failGet = false; failPost = false;
  await flush(() => button(host, 'Try again').click());
  assert.equal(count(), expectedCount);
  setLang('es', { persist: false });
  await flush(() => root.render(React.createElement(PluginMarketplace, { key: 'spanish' })));
  assert.equal(host.querySelector('h2').textContent, 'Marketplace de plugins');
  assert.ok(host.textContent.includes('Buscar un plugin'));
  assert.ok(row('hoardhub').textContent.includes('Obligatoria'));
  assert.equal(count(), expectedCount);
  await flush(() => root.unmount());
  // Execute the actual preview, including its manifest resolution, rather than only compiling it.
  const previewBuild = await build({ entryPoints: ['studio/checks/plugin-marketplace-preview.tsx'],
    bundle: true, jsx: 'automatic', format: 'esm', platform: 'node', packages: 'external',
    write: false, loader: { '.css': 'empty' } });
  writeFileSync(previewOutput, previewBuild.outputFiles[0].text);
  const fixture = document.createElement('div'); fixture.id = 'fixture'; document.body.append(fixture);
  Object.defineProperty(globalThis, 'location', { value: dom.location, configurable: true });
  globalThis.fetch = (...args) => window.fetch(...args);
  let preview;
  await flush(async () => { preview = await import(pathToFileURL(previewOutput)); });
  assert.equal(preview.previewManifestRegistry.size, expectedCount);
  for (const row of makeCatalogue().plugins) {
    const manifest = preview.previewManifestRegistry.get(row.id);
    assert.ok(manifest, `preview registry missing ${row.id}`);
    assert.equal(manifest.name, row.name);
    assert.equal(preview.catalogue.plugins.find(item => item.id === row.id).name, row.name);
  }
  assert.equal(fixture.querySelectorAll('.fs-conn__list > li').length, expectedCount);
  await flush(() => preview.previewRoot.unmount());
  console.log(`ALL OK: ${expectedCount} plugins, required filter, install+refresh, cloned configure path, trimmed link, unlink contract+notice, failures retain catalogue, initial retry, ES/EN, executed preview registry coverage`);
} finally {
  unlinkSync(output);
  try { unlinkSync(previewOutput); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  await dom.happyDOM.close();
}
