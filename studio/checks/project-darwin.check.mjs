// Darwin's Hoard on the project overview: a card fed by GET /api/hoard/darwin/panel (Darwin's darwin_panel through the
// hub, so the browser stays same-origin), shown when a Darwin connector exists. Static source assertions plus the two
// URL builders run for real (type annotations stripped), the same style as apps.check.mjs.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const src = readFileSync(new URL('../src/screens/project/ProjectDarwin.tsx', import.meta.url), 'utf8');
const page = readFileSync(new URL('../src/screens/Project.tsx', import.meta.url), 'utf8');
const es = readFileSync(new URL('../src/i18n/es.ts', import.meta.url), 'utf8');
const route = readFileSync(new URL('../../routes/hoard_hub_routes.py', import.meta.url), 'utf8');

assert.match(src, /export function ProjectOverviewDarwin/, 'the card component exists');
assert.match(src, /listConnectors\(\)/, 'it finds Darwin among the connectors');
assert.match(src, /=== 'darwin'/, 'by its preset id');
assert.doesNotMatch(src, /<iframe/, 'no frame: the data comes through Faustus');
assert.match(src, /getJson<DarwinPanel>\(darwinPanelPath/, 'it reads the panel through Faustus');
assert.match(route, /@router\.get\('\/api\/hoard\/darwin\/panel'\)/, 'the backend route exists');
assert.match(route, /family\.call, 'darwin', 'darwin_panel'/, 'and calls Darwin through the hub');
assert.match(page, /<ProjectOverviewDarwin project=\{project\} \/>/, 'the project overview shows it');
for (const key of ['Open Darwin', 'Worth a look', 'The whole family', 'Next radar searches']) assert.ok(es.includes(`"${key}":`), `Spanish for ${key}`);

const fn = (name, sig) => {
  const m = src.match(new RegExp(`export function ${name}\\(([\\s\\S]*?)\\n}\\n`));
  assert.ok(m, `${name} is defined`);
  return m[0].replace('export function', 'function').replace(sig[0], sig[1]);
};
const LOOPBACK = ['127.0.0.1', 'localhost', '[::1]', '::1'];
const darwinLink = new Function('LOOPBACK', `${fn('darwinLink', [/\(appUrl: string, params: \{ app\?: string; open\?: number \}, host: string\): string/, '(appUrl, params, host)'])}; return darwinLink;`)(LOOPBACK);
const darwinPanelPath = new Function(`${fn('darwinPanelPath', [/\(space: string \| undefined, project: string\): string/, '(space, project)'])}; return darwinPanelPath;`)();

const u = new URL(darwinLink('http://127.0.0.1:5233/x?y=1', { app: 'silver', open: 42 }, 'localhost'));
assert.equal(u.host, 'localhost:5233', 'follows the loopback host Studio is open on');
assert.equal(u.searchParams.get('app'), 'silver');
assert.equal(u.searchParams.get('open'), '42');
assert.equal(u.hash, '#history');
assert.equal(new URL(darwinLink('http://nas.local:5233', {}, '127.0.0.1')).host, 'nas.local:5233', 'a LAN URL is kept');
assert.equal(darwinPanelPath('silver', "Silver's Hoard"), '/api/hoard/darwin/panel?project=Silver%27s+Hoard&space=silver');
assert.equal(darwinPanelPath(undefined, 'Research'), '/api/hoard/darwin/panel?project=Research');
console.log('project-darwin: ok');
