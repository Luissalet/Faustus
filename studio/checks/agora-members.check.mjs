/** Render the real member list and open its cards against old and new Hub responses. */
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const temporary = mkdtempSync(join(tmpdir(), 'agora-members-'));
const window = new Window({ url: 'https://studio.faustus.invalid/' });
for (const key of ['window', 'document', 'HTMLElement', 'SVGElement', 'Node', 'NodeFilter', 'Element', 'Event', 'CustomEvent',
  'MouseEvent', 'localStorage', 'MutationObserver', 'getComputedStyle', 'DocumentFragment', 'requestAnimationFrame',
  'cancelAnimationFrame', 'HTMLInputElement', 'HTMLSelectElement', 'HTMLTextAreaElement', 'FocusEvent', 'KeyboardEvent', 'PointerEvent']) {
  if (key in window) globalThis[key] = window[key];
}
document.body.innerHTML = '<div id="root"></div><div id="fs-overlay-root"></div>';
const waitFor = async (probe) => {
  const until = Date.now() + 3000;
  while (!probe() && Date.now() < until) await new Promise((r) => setTimeout(r, 10));
  assert.ok(probe(), 'the expected render settled');
};
const member = (id, extra = {}) => ({ id, name: id, kind: 'agent', role: 'codex', presence: 'working', state: 'working',
  doing: 'Reviewing changes', note: '', last_seen: Date.now() / 1000, locks: 0, tasks: [], quota: { state: 'unknown', note: null }, ...extra });
const guild = (members) => ({ space: { id: 'test', title: 'Test' }, members, providers: [], tasks: {}, threads: [] });
let consoleReply = { leaders: [], workers: [] };
let consoleUnavailable = false;
globalThis.fetch = async () => {
  if (consoleUnavailable) throw new Error('Console unavailable');
  return { ok: true, json: async () => consoleReply };
};

try {
  const outfile = join(temporary, 'check.mjs');
  await build({ stdin: { contents: `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { flushSync } from 'react-dom';
    import { MembersPane } from './studio/src/screens/agora/MembersPane';
    import { setLang } from './studio/src/i18n';
    const root = createRoot(document.getElementById('root'));
    let version = 0;
    export function mount(guild, lang) {
      setLang(lang, { persist: false });
      flushSync(() => root.render(<MembersPane key={++version} guild={guild} onOpen={() => {}} notify={() => {}} onChanged={() => {}} />));
    }
    export function unmount() { root.unmount(); }
  `, resolveDir: root, loader: 'tsx' }, bundle: true, format: 'esm', platform: 'browser', outfile,
    jsx: 'automatic', loader: { '.css': 'empty' }, define: { 'process.env.NODE_ENV': '"production"' }, logLevel: 'silent' });
  const app = await import(pathToFileURL(outfile));
  const row = (id) => document.querySelector(`[data-testid="agora-member-${id}"]`);
  const model = () => document.querySelector('[data-testid="agora-profile-model"]');
  const open = async (id) => {
    row(id).click();
    await waitFor(model);
    return model();
  };
  const declaration = { declared_model: 'provider::Declared model', declared_effort: 'high', declared_at: '2026-11-14T01:00:00Z' };

  // An older Hub, including an unavailable console, still opens every agent card.
  consoleUnavailable = true;
  app.mount(guild([member('codex-old')]), 'en');
  await waitFor(() => row('codex-old'));
  assert.equal(row('codex-old').querySelector('[data-testid="agora-member-model"]'), null);
  assert.equal((await open('codex-old')).textContent, 'Model · effortnot declared');

  app.mount(guild([member('codex-declared', declaration)]), 'es');
  await waitFor(() => row('codex-declared'));
  assert.equal(row('codex-declared').querySelector('[data-testid="agora-member-model"]').textContent, 'Declared model');
  assert.ok(row('codex-declared').textContent.includes('Reviewing changes'), 'existing status stays visible');
  let card = await open('codex-declared');
  assert.match(card.textContent, /Modelo · esfuerzoDeclared model · Alto.*declarado por el agente/);
  assert.equal(card.querySelector('time').dateTime, declaration.declared_at);

  // Verified session settings win as a pair, including the newest run for a reused identity.
  consoleUnavailable = false;
  consoleReply = { leaders: [{ id: 'codex', catalog: [{ id: 'verified', name: 'Verified model' }] }], workers: [
    { agent_id: 'codex-verified', parent_id: 'codex', model: 'verified', effort: 'low', state: 'completed' },
    { agent_id: 'codex-verified', parent_id: 'codex', model: 'old-model', effort: 'high', state: 'completed' },
  ] };
  app.mount(guild([member('codex-verified', declaration)]), 'en');
  await waitFor(() => row('codex-verified')?.textContent.includes('Verified model'));
  card = await open('codex-verified');
  assert.equal(card.textContent, 'Model · effortVerified model · Low');
  assert.equal(card.querySelector('time'), null);
  assert.ok(document.querySelector('[data-testid="agora-profile"]').textContent.includes('Its session finished'));

  consoleReply.workers[0].effort = '';
  app.mount(guild([member('codex-verified', declaration)]), 'en');
  await waitFor(() => row('codex-verified')?.textContent.includes('Verified model'));
  assert.equal((await open('codex-verified')).textContent, 'Model · effortVerified model · not declared');

  // Partial declarations and malformed optional dates never hide the known field or crash the card.
  consoleReply = { leaders: [], workers: [] };
  app.mount(guild([member('codex-partial', { declared_effort: 'ultra', declared_at: 'invalid' })]), 'en');
  await waitFor(() => row('codex-partial'));
  card = await open('codex-partial');
  assert.equal(card.textContent, 'Model · effortnot declared · Ultradeclared by the agent');
  assert.equal(card.querySelector('time'), null);

  app.mount(guild([member('person', { role: 'person', presence: 'person', ...declaration })]), 'es');
  await waitFor(() => row('person'));
  row('person').click();
  await waitFor(() => document.querySelector('[data-testid="agora-profile"]'));
  assert.equal(model(), null, 'person cards have no agent model metadata');
  app.unmount();
  console.log('Agora member cards: old Hub, declarations, dates, verified priority, partial fields, EN/ES and person cards passed.');
} finally {
  window.happyDOM.abort();
  rmSync(temporary, { recursive: true, force: true });
}
