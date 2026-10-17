import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { Window } from 'happy-dom';

const window = new Window({ url: 'http://localhost/' });
window.localStorage.setItem('faustus_studio_lang', 'es');
for (const key of ['window', 'document', 'navigator', 'HTMLElement', 'MutationObserver', 'Event', 'CustomEvent', 'getComputedStyle']) Object.defineProperty(globalThis, key, { configurable: true, value: key === 'getComputedStyle' ? window.getComputedStyle.bind(window) : key === 'window' ? window : window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = callback => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = clearTimeout;
const built = await build({ stdin: { contents: `export { PersonalAssistants } from './studio/src/screens/home/PersonalAssistants'; export { createElement, act } from 'react'; export { createRoot } from 'react-dom/client'; export { MemoryRouter } from 'react-router';`, resolveDir: process.cwd(), loader: 'ts' }, bundle: true, format: 'esm', platform: 'node', write: false, loader: { '.css': 'empty' } });
const m = await import('data:text/javascript;base64,' + Buffer.from(built.outputFiles[0].text).toString('base64'));
const props = node => node[Object.keys(node).find(k => k.startsWith('__reactProps$'))];
const host = document.createElement('div'); document.body.append(host);
const root = m.createRoot(host);
const rows = [{ id: 'a', session_id: 'a', name: 'Ayudante', description: 'Notas', instructions: 'ROLE', skills: ['check'], state: 'ready', proactivity: { enabled: false, prompt: 'Check news', schedule: 'weekly', time: '10:30', day: 2, timezone: 'Europe/Madrid', cron: '0 9 * * *', notifications: false, quiet: true } }];
let finish;
const requests = [];
globalThis.fetch = async (url, init) => {
  if (init?.method === 'PATCH') { requests.push(JSON.parse(init.body)); return new Promise(resolve => { finish = resolve; }); }
  if (url.endsWith('/assistants/skills')) return Response.json({ skills: [{ name: 'check', description: 'Check primary sources' }] });
  if (url.endsWith('/api/projects')) return Response.json({ projects: [] });
  if (url.includes('/api/projects')) return Response.json({ projects: [] });
  if (url.includes('/sessions')) return Response.json({ sessions: [] });
  return Response.json({ assistants: rows });
};
try {
  await m.act(async () => root.render(m.createElement(m.MemoryRouter, null, m.createElement(m.PersonalAssistants))));
  await m.act(async () => host.querySelector('[aria-label="Editar asistente"]').click());
  const field = text => [...host.querySelectorAll('label')].find(n => n.textContent.startsWith(text)).querySelector('input,textarea,select');
  assert.equal(field('Instrucciones de trabajo').value, 'ROLE');
  assert.equal(field('Activar proactividad').checked, false, 'editing never activates a schedule');
  await m.act(async () => props(field('Instrucciones de trabajo')).onChange({ target: { value: 'Updated instructions' } }));
  await m.act(async () => host.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })));
  assert.equal(requests[0].instructions, 'Updated instructions');
  assert.deepEqual(requests[0].skills, ['check']);
  assert.equal(requests[0].proactivity.enabled, false);
  assert.equal(requests[0].proactivity.prompt, 'Check news', 'pausing retains the mission');
  assert.equal(requests[0].proactivity.timezone, 'Europe/Madrid');
  assert.ok(field('Instrucciones de trabajo').disabled, 'saving protects newer typing from being discarded');
  await m.act(async () => finish(Response.json({ detail: 'Schedule changed; try again' }, { status: 409 })));
  assert.equal(field('Instrucciones de trabajo').value, 'Updated instructions', 'failed saves retain the draft');
  assert.match(host.querySelector('[role="alert"]').textContent, /Schedule changed/);
  assert.equal(field('Instrucciones de trabajo').disabled, false);
  await m.act(async () => props(field('Activar proactividad')).onChange({ target: { checked: true } }));
  assert.equal(field('Misión proactiva').value, 'Check news');
  assert.equal(field('Día de la semana').value, '2');
  await m.act(async () => host.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })));
  assert.equal(requests[1].proactivity.enabled, true);
  await m.act(async () => finish(Response.json(rows[0])));
  assert.equal(host.querySelector('form'), null);
  console.log('PASS: mounted assistant configuration, explicit activation, selected skills, pending-save protection and draft recovery');
} finally { await m.act(async () => root.unmount()); window.happyDOM.abort(); }
