/** Render the agent page against fake hub answers: header, tabs, tell, proposals and the layer that opens it. */
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const temporary = mkdtempSync(join(tmpdir(), 'agent-page-'));
const window = new Window({ url: 'https://studio.faustus.invalid/' });
for (const key of ['window', 'document', 'HTMLElement', 'SVGElement', 'Node', 'NodeFilter', 'Element', 'Event', 'CustomEvent',
  'MouseEvent', 'localStorage', 'MutationObserver', 'getComputedStyle', 'DocumentFragment', 'requestAnimationFrame',
  'cancelAnimationFrame', 'HTMLInputElement', 'HTMLSelectElement', 'HTMLTextAreaElement', 'FocusEvent', 'KeyboardEvent', 'PointerEvent']) {
  if (key in window) globalThis[key] = window[key];
}
document.body.innerHTML = '<div id="root"></div><div id="fs-overlay-root"></div>';
const waitFor = async (probe, what = 'the expected render') => {
  const until = Date.now() + 3000;
  while (!probe() && Date.now() < until) await new Promise((r) => setTimeout(r, 10));
  assert.ok(probe(), what + ' settled');
};

const now = Date.now() / 1000;
const proposal = { id: 4, agent: 'claude-x', space_id: 'ecosystem', body: 'A\nB', base_body: 'A', reason: 'ExampleUser corrected me', trigger: 'correction',
  source: 'task 9', status: 'pending', created: now, decided_by: null, decided_at: null, decision_note: '', stale: false, diff: '@@\n+B' };
const profile = (extra = {}) => ({ ok: true,
  profile: { id: 'claude-x', name: 'claude-x', initials: 'CX', kind: 'agent', provider: 'claude', engine: 'Claude', model: 'sonnet', effort: 'high',
    model_source: 'console', rank: 'lead', presence: 'working', state: 'working', doing: 'Writing tests', note: '', last_seen: now, first_seen: now - 99,
    since: now - 120, since_source: 'session', color: null, locks: 0, in_server: true, tasks_in_hand: [], quota: {} },
  activity: { messages: [], tasks: [], deliveries: [], commits: [], timeline: [
    { at: now, type: 'message', text: 'hello team', where: '#general', message_id: 1 },
    { at: now, type: 'delivery', text: 'A delivery', task_id: 9, commits: ['abcdef1234'], status: 'review' }] },
  instructions: { body: 'A', updated_by: 'example-user', updated: now },
  memory: [{ id: 3, kind: 'correction', text: 'No trailers', source: 'task 9', by: 'example-user', at: now, space_id: 'ecosystem' }],
  routines: [{ name: 'Inbox sweep', schedule: 'every 2 h', note: '', enabled: true }],
  usage: { days: Array.from({ length: 14 }, (_, i) => ({ day: `2026-11-${String(i + 1).padStart(2, '0')}`, messages: i, tasks_closed: 0, run_minutes: 0 })), tokens: null, note: '' },
  proposals: [proposal], proposals_pending: 1, dm: { channel: 'dm-abc', thread_id: 7 }, ...extra });
const calls = [];
let reply = profile();
globalThis.fetch = async (url, init = {}) => {
  const path = String(url);
  calls.push({ path, body: init.body ? JSON.parse(init.body) : null });
  if (path.includes('agent_profile')) return { ok: true, json: async () => reply };
  if (path.includes('agent_tell')) return { ok: true, json: async () => ({ ok: true, delivery: { status: 'queued', detail: '', wake: { status: 'queued' } } }) };
  return { ok: true, json: async () => ({ ok: true, proposal: { ...proposal, status: 'accepted' } }) };
};

try {
  const outfile = join(temporary, 'check.mjs');
  await build({ stdin: { contents: `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { flushSync } from 'react-dom';
    import { AgentPageLayer, openAgentPage } from './studio/src/screens/agent-page/AgentPageLayer';
    import { setLang } from './studio/src/i18n';
    const root = createRoot(document.getElementById('root'));
    export function mount(lang, onOpenDm) {
      setLang(lang, { persist: false });
      flushSync(() => root.render(<><button data-agent-id="claude-x" id="who">claude-x</button><AgentPageLayer space="ecosystem" onOpenDm={onOpenDm} /></>));
    }
    export { openAgentPage };
    export function unmount() { root.unmount(); }
  `, resolveDir: root, loader: 'tsx' }, bundle: true, format: 'esm', platform: 'browser', outfile,
    jsx: 'automatic', loader: { '.css': 'empty' }, define: { 'process.env.NODE_ENV': '"production"' }, logLevel: 'silent' });
  const app = await import(pathToFileURL(outfile));
  const q = (id) => document.querySelector(`[data-testid="${id}"]`);
  let opened = '';
  app.mount('en', (slug) => { opened = slug; });
  assert.equal(q('agent-page'), null, 'nothing is open until asked');

  // A click on anything that carries data-agent-id opens the page of that agent.
  document.getElementById('who').click();
  await waitFor(() => q('agent-page'), 'the page');
  const text = q('agent-page').textContent;
  assert.match(text, /claude-x/);
  assert.match(text, /Claude · sonnet · high/);
  assert.match(text, /Lead/);
  assert.match(q('agent-activity').textContent, /hello team/);
  assert.match(q('agent-activity').textContent, /abcdef12/);
  assert.ok(q('agent-proposals-banner'), 'a pending proposal is announced outside its tab');

  // Tabs.
  q('agent-tab-instructions').click();
  await waitFor(() => q('agent-instructions'), 'the instructions tab');
  assert.match(q('agent-instructions').textContent, /ExampleUser corrected me/);
  assert.equal(q('agent-instructions-text').value, 'A');
  q('agent-tab-memory').click();
  await waitFor(() => q('agent-memory'), 'the memory tab');
  assert.match(q('agent-memory').textContent, /No trailers/);
  q('agent-tab-usage').click();
  await waitFor(() => q('agent-usage'), 'the usage tab');
  assert.equal(document.querySelectorAll('.fs-agp-bars__day').length, 14);

  // Accepting a proposal asks the hub to decide, as accept.
  q('agent-tab-instructions').click();
  await waitFor(() => q('agent-proposal-accept'), 'the proposal');
  q('agent-proposal-accept').click();
  await waitFor(() => calls.some((c) => c.path.endsWith('agent_instruction_decide')), 'the decision call');
  const decide = calls.find((c) => c.path.endsWith('agent_instruction_decide'));
  assert.deepEqual([decide.body.proposal_id, decide.body.decision], [4, 'accept']);

  // Telling the agent something sends it, marking a correction when asked.
  const area = q('agent-tell-text');
  Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(area, 'Please run the tests first');
  area.dispatchEvent(new window.Event('input', { bubbles: true }));
  document.querySelector('.fs-agp-check input').click();
  await waitFor(() => !q('agent-tell-send').disabled, 'the send button');
  q('agent-tell-send').click();
  await waitFor(() => calls.some((c) => c.path.endsWith('agent_tell')), 'the tell call');
  const tell = calls.find((c) => c.path.endsWith('agent_tell'));
  assert.equal(tell.body.to, 'claude-x');
  assert.equal(tell.body.text, 'Please run the tests first');
  assert.equal(tell.body.correction, true);

  // The conversation button opens the direct channel; Escape closes the page.
  [...document.querySelectorAll('.fs-agp-head__actions button')].find((b) => /Direct messages/.test(b.textContent)).click();
  assert.equal(opened, 'dm-abc');
  await waitFor(() => !q('agent-page'), 'the page closing');

  // In Spanish, and for an agent the hub does not know.
  app.mount('es');
  app.openAgentPage('claude-x');
  await waitFor(() => q('agent-page'), 'the Spanish page');
  assert.match(q('agent-tell').textContent, /Decirle algo a claude-x/);
  assert.match(q('agent-tab-activity').textContent, /Actividad/);
  document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
  await waitFor(() => !q('agent-page'), 'Escape');
  globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({ ok: false, error: 'unknown agent' }) });
  app.openAgentPage('ghost-agent');
  await waitFor(() => q('agent-page-error'), 'the error');
  assert.match(q('agent-page-error').textContent, /unknown agent/);
  app.unmount();
  console.log('Agent page: header, tabs, proposals, tell, direct channel, layer, EN/ES and an unknown agent passed.');
} finally {
  window.happyDOM.abort();
  rmSync(temporary, { recursive: true, force: true });
}
