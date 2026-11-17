/** Render the router's line under messages and its settings in the server dialog against old and new Hub answers. */
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const temporary = mkdtempSync(join(tmpdir(), 'agora-route-'));
const window = new Window({ url: 'https://studio.faustus.invalid/' });
for (const key of ['window', 'document', 'HTMLElement', 'SVGElement', 'Node', 'NodeFilter', 'Element', 'Event', 'CustomEvent',
  'MouseEvent', 'localStorage', 'MutationObserver', 'getComputedStyle', 'DocumentFragment', 'requestAnimationFrame',
  'cancelAnimationFrame', 'HTMLInputElement', 'HTMLSelectElement', 'HTMLTextAreaElement', 'FocusEvent', 'KeyboardEvent', 'PointerEvent']) {
  if (key in window) globalThis[key] = window[key];
}
document.body.innerHTML = '<div id="root"></div><div id="fs-overlay-root"></div>';
globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} };
globalThis.AbortController = window.AbortController;
const waitFor = async (probe, what = 'the expected render') => {
  const until = Date.now() + 3000;
  while (!probe() && Date.now() < until) await new Promise((r) => setTimeout(r, 10));
  assert.ok(probe(), what + ' settled');
};
// The line is flex items spaced by `gap`: its words are its children's.
const said = (el) => [...el.children].map((c) => c.textContent.trim()).filter(Boolean).join(' ');
const q = (id) => document.querySelector(`[data-testid="${id}"]`);

// What the hub answers: settings of the server (or nothing, when it is an older hub) and what is written.
let hub = { settings: { enabled: true, max_agents: 3, model: 'auto' }, missing: false };
const calls = [];
globalThis.fetch = async (url, init) => {
  const path = String(url);
  const body = init?.body ? JSON.parse(init.body) : null;
  calls.push({ path, method: init?.method || 'GET', body });
  const reply = (status, data) => ({ ok: status < 400, status, json: async () => data });
  if (path.startsWith('/api/agora/x/route_settings_set')) {
    hub.settings = { ...hub.settings, ...Object.fromEntries(Object.entries(body).filter(([k]) => k !== 'space_id')) };
    return reply(200, { ok: true, settings: hub.settings });
  }
  if (path.startsWith('/api/agora/x/route_settings')) {
    return hub.missing ? reply(404, { ok: false, error: 'unknown Agora route' }) : reply(200, { ok: true, settings: hub.settings, models: ['auto', 'rules'] });
  }
  if (path.startsWith('/api/agora/where')) return reply(200, { remote: false });
  if (path.includes('/api/agora/x/')) return reply(200, { ok: true });
  return reply(200, {});
};

try {
  const outfile = join(temporary, 'check.mjs');
  await build({ stdin: { contents: `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { flushSync } from 'react-dom';
    import { RouteLine } from './studio/src/screens/agora/Router';
    import { EditServerDialog } from './studio/src/screens/agora/ServerDialogs';
    import { setLang } from './studio/src/i18n';
    const root = createRoot(document.getElementById('root'));
    let version = 0;
    export function line(route, lang) {
      setLang(lang, { persist: false });
      flushSync(() => root.render(<div key={++version}><RouteLine route={route} /></div>));
    }
    export function dialog(guild, lang, saved) {
      setLang(lang, { persist: false });
      flushSync(() => root.render(<EditServerDialog key={++version} open guild={guild} onClose={() => {}} onSaved={saved} notify={() => {}} />));
    }
    export function unmount() { root.unmount(); }
  `, resolveDir: root, loader: 'tsx' }, bundle: true, format: 'esm', platform: 'browser', outfile,
    jsx: 'automatic', loader: { '.css': 'empty' }, define: { 'process.env.NODE_ENV': '"production"' }, logLevel: 'silent' });
  const app = await import(pathToFileURL(outfile));

  // A message the router chose for: who was told, who passed, and why, in both languages.
  const route = {
    label: 'Lo atiende @claude-x · elegido por el enrutador (modelo)', engine: 'model', note: 'Two candidates were close.',
    picks: [{ agent: 'claude-x', why: 'owns task #12', status: 'assigned' }, { agent: 'codex-y', why: 'quota is low', status: 'passed' }],
  };
  app.line(route, 'en');
  await waitFor(() => q('agora-route'), 'the route line');
  assert.equal(said(q('agora-route')), 'Answered by @claude-x @codex-y · chosen by the router (model)');
  const picks = [...document.querySelectorAll('[data-testid="agora-route-pick"]')];
  assert.deepEqual(picks.map((p) => p.getAttribute('data-agent-id')), ['claude-x', 'codex-y'], 'each pick opens its agent page');
  assert.deepEqual(picks.map((p) => p.getAttribute('data-status')), ['assigned', 'passed'], 'the one that passed is marked');
  assert.equal(picks[0].title, 'owns task #12', 'the reason is in the title');
  assert.match(picks[1].getAttribute('aria-label'), /codex-y, passed\. quota is low/);
  assert.equal(q('agora-route').title, route.label, "the hub's own wording stays as the line's title");
  q('agora-route-how').click();
  await waitFor(() => q('agora-route-pop'), 'the reasons popover');
  assert.match(q('agora-route-pop').textContent, /owns task #12/);
  assert.match(q('agora-route-pop').textContent, /codex-y.*passed.*quota is low/);
  assert.match(q('agora-route-pop').textContent, /Two candidates were close\./);

  app.line({ ...route, engine: 'rules' }, 'es');
  await waitFor(() => q('agora-route'), 'the Spanish route line');
  assert.equal(said(q('agora-route')), 'Lo atiende @claude-x @codex-y · elegido por el enrutador (reglas)');

  // Nobody has to answer; and shapes an older or newer hub could send never break a message.
  app.line({ label: 'Nadie tiene que contestar · decidido por el enrutador', engine: 'model', picks: [], note: 'A thanks.' }, 'en');
  await waitFor(() => q('agora-route'), 'the nobody line');
  assert.equal(said(q('agora-route')), 'Nobody has to answer · chosen by the router (model)');
  assert.equal(q('agora-route').getAttribute('data-nobody'), 'true');
  assert.equal(document.querySelectorAll('[data-testid="agora-route-pick"]').length, 0);
  app.line({ label: '', engine: 'future', picks: [{ agent: 'a-1', why: '', status: 'assigned' }, { nope: true }, null] }, 'en');
  await waitFor(() => q('agora-route'), 'an unknown engine');
  assert.match(said(q('agora-route')), /Answered by @a-1 · decided by the router/);
  assert.equal(document.querySelectorAll('[data-testid="agora-route-pick"]').length, 1, 'malformed picks are left out');
  for (const none of [null, undefined, 'x']) {
    app.line(none, 'en');
    await new Promise((r) => setTimeout(r, 5));
    assert.equal(q('agora-route'), null, 'a message without a route draws nothing');
  }

  // The settings of the server, in its edit dialog: loaded, changed and written, only what changed.
  const guild = { space: { id: 'faustus', title: 'Faustus', description: '', project_id: '', workspace: '', revision: 3,
    meta: { icon: '', color: '', folders: [], position: null, hidden: false } }, members: [], channels: [], threads: [], tasks: {}, providers: [] };
  let saved = 0;
  app.dialog(guild, 'en', () => { saved += 1; });
  await waitFor(() => q('agora-router-enabled'), 'the router settings');
  assert.equal(q('agora-router-enabled').checked, true);
  assert.equal(q('agora-router-max').value, '3');
  assert.equal(q('agora-router-model').value, 'auto');
  assert.deepEqual([...q('agora-router-max').options].map((o) => o.value), ['1', '2', '3', '4', '5']);
  assert.deepEqual([...q('agora-router-model').options].map((o) => o.textContent), ['The Sparks model', 'Rules only']);
  const change = (el, value) => {
    const proto = el.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, el.type === 'checkbox' ? 'checked' : 'value').set.call(el, value);
    el.dispatchEvent(new window.Event(el.tagName === 'SELECT' || el.type === 'checkbox' ? 'change' : 'input', { bubbles: true }));
  };
  // Saving without touching them writes nothing to the router.
  q('agora-save-server').click();
  await waitFor(() => saved === 1, 'a save without router changes');
  assert.equal(calls.filter((c) => c.path.includes('route_settings_set')).length, 0);

  app.dialog(guild, 'es', () => { saved += 1; });
  await waitFor(() => q('agora-router-max'), 'the Spanish settings');
  assert.match(q('agora-router-settings').textContent, /Quién contesta los mensajes que no nombran a nadie/);
  assert.deepEqual([...q('agora-router-model').options].map((o) => o.textContent), ['El modelo de las Sparks', 'Solo reglas']);
  change(q('agora-router-max'), '5');
  change(q('agora-router-model'), 'rules');
  await waitFor(() => q('agora-router-model').value === 'rules', 'the changed model');
  q('agora-save-server').click();
  await waitFor(() => saved === 2, 'the save');
  const wrote = calls.filter((c) => c.path.includes('route_settings_set'));
  assert.equal(wrote.length, 1);
  assert.deepEqual(wrote[0].body, { space_id: 'faustus', max_agents: 5, model: 'rules' }, 'only what changed is sent');
  assert.deepEqual(hub.settings, { enabled: true, max_agents: 5, model: 'rules' });

  // Off: its other controls stop; the switch alone is what is sent.
  app.dialog(guild, 'en', () => { saved += 1; });
  await waitFor(() => q('agora-router-max') && q('agora-router-max').value === '5', 'the new settings');
  q('agora-router-enabled').click();
  await waitFor(() => q('agora-router-max').disabled, 'the disabled controls');
  assert.equal(q('agora-router-model').disabled, true);
  calls.length = 0;
  q('agora-save-server').click();
  await waitFor(() => saved === 3, 'turning it off');
  assert.deepEqual(calls.filter((c) => c.path.includes('route_settings_set')).map((c) => c.body), [{ space_id: 'faustus', enabled: false }]);

  // An older Hub without the router: the section says so and the server still saves.
  hub = { settings: null, missing: true };
  app.dialog(guild, 'en', () => { saved += 1; });
  await waitFor(() => q('agora-router-unavailable'), 'the notice for an older hub');
  assert.equal(q('agora-router-enabled'), null);
  q('agora-save-server').click();
  await waitFor(() => saved === 4, 'saving against an older hub');

  app.unmount();
  console.log('Agora router: route line (picks, passed, nobody, unknown shapes, EN/ES), popover, settings load/save/off and older Hub passed.');
} finally {
  window.happyDOM.abort();
  rmSync(temporary, { recursive: true, force: true });
}
