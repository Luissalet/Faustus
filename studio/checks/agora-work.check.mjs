/** Render the tools an agent used under its message, the routine tag and the live steps of a task card, in both languages. */
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const temporary = mkdtempSync(join(tmpdir(), 'agora-work-'));
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
const q = (id) => document.querySelector(`[data-testid="${id}"]`);
const all = (id) => [...document.querySelectorAll(`[data-testid="${id}"]`)];
const flat = (el) => el.textContent.replace(/\s+/g, ' ').trim();

// What the hub answers when a long work asks for the rest of its tools.
const hub = { whole: [], fail: false };
const calls = [];
globalThis.fetch = async (url) => {
  const path = String(url);
  calls.push(path);
  const reply = (status, data) => ({ ok: status < 400, status, json: async () => data });
  if (path.startsWith('/api/agora/x/work')) {
    return hub.fail ? reply(500, { ok: false, error: 'boom' })
      : reply(200, { ok: true, has_work: true, message: { id: 7, work: { count: hub.whole.length, tools: hub.whole, more: 0 } } });
  }
  return reply(200, {});
};

try {
  const outfile = join(temporary, 'check.mjs');
  await build({ stdin: { contents: `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { flushSync } from 'react-dom';
    import { WorkCard, RoutineTag, LiveSteps, span } from './studio/src/screens/agora/Work';
    import { setLang } from './studio/src/i18n';
    const root = createRoot(document.getElementById('root'));
    let version = 0;
    export const card = (props) => <WorkCard space="faustus" {...props} />;
    export const routine = (value) => <RoutineTag routine={value} />;
    export const steps = (live, now) => <LiveSteps live={live} now={now} />;
    export function draw(node, lang) {
      setLang(lang, { persist: false });
      flushSync(() => root.render(<div key={++version}>{node}</div>));
    }
    export { span };
    export function unmount() { root.unmount(); }
  `, resolveDir: root, loader: 'tsx' }, bundle: true, format: 'esm', platform: 'browser', outfile,
    jsx: 'automatic', loader: { '.css': 'empty' }, define: { 'process.env.NODE_ENV': '"production"' }, logLevel: 'silent' });
  const app = await import(pathToFileURL(outfile));

  const tools = [
    { name: 'Bash', ok: true, ms: 1200, summary: 'pytest tests -q' },
    { name: 'Edit', ok: true, ms: 40, summary: 'src/a.py' },
    { name: 'mcp__hub__hub_agora_post', ok: false, ms: 90_000, summary: 'say hola' },
  ];
  const work = { count: 3, ok: 2, failed: 1, duration_s: 120, started: 1, ended: 121, source: 'observed', tools, more: 0 };

  // Folded: how many tools and how long, the icons of what was used and the failures; nothing of the list yet.
  app.draw(app.card({ work, messageId: 7 }), 'en');
  await waitFor(() => q('agora-work'), 'the work card');
  assert.equal(flat(q('agora-work-line')), 'Used 3 tools · 2 min');
  assert.equal(flat(q('agora-work-failed')), '1 failed');
  assert.equal(q('agora-work-toggle').getAttribute('aria-expanded'), 'false');
  assert.equal(q('agora-work-list'), null);
  assert.equal(document.querySelectorAll('.fs-agx-work__icons svg').length, 3, 'one icon per kind of tool used');

  // Opened: the tools in order with their summary, duration and result.
  q('agora-work-toggle').click();
  await waitFor(() => q('agora-work-list'), 'the list');
  const rows = all('agora-work-tool');
  assert.deepEqual(rows.map((r) => r.querySelector('code').textContent), ['Bash', 'Edit', 'mcp__hub__hub_agora_post']);
  assert.deepEqual(rows.map((r) => r.getAttribute('data-ok')), ['true', 'true', 'false']);
  assert.match(flat(rows[0]), /pytest tests -q.*1 s/);
  assert.match(flat(rows[2]), /2 min/, 'a long call shows minutes');
  assert.equal(q('agora-work-toggle').getAttribute('aria-expanded'), 'true');
  assert.equal(calls.length, 0, 'a whole work asks for nothing more');

  // The same, in Spanish, with one tool and with three.
  app.draw(app.card({ work: { ...work, count: 1, failed: 0, ok: 1, duration_s: 45, tools: [tools[0]] }, messageId: 7 }), 'es');
  await waitFor(() => q('agora-work'), 'the Spanish card');
  assert.equal(flat(q('agora-work-line')), '1 herramienta usada · 45 s');
  assert.equal(q('agora-work-failed'), null);
  app.draw(app.card({ work, messageId: 7 }), 'es');
  await waitFor(() => q('agora-work-line'), 'the Spanish count');
  assert.equal(flat(q('agora-work-line')), '3 herramientas usadas · 2 min');
  assert.equal(flat(q('agora-work-failed')), '1 fallidos');

  // A long work: the page carried the first tools; opening it asks for the rest.
  hub.whole = [...tools, { name: 'Grep', ok: true, ms: 5, summary: 'buscar' }, { name: 'Read', ok: null, ms: null, summary: '' }];
  app.draw(app.card({ work: { ...work, count: 5, tools: tools.slice(0, 2), more: 3 }, messageId: 7 }), 'en');
  await waitFor(() => q('agora-work-toggle'), 'the long card');
  q('agora-work-toggle').click();
  await waitFor(() => all('agora-work-tool').length === 5, 'the rest of the tools');
  assert.ok(calls.some((c) => c.startsWith('/api/agora/x/work') && c.includes('message_id=7') && c.includes('space_id=faustus')));

  // ...and when the hub cannot give the rest, the card says how many are not shown and keeps what it has.
  hub.fail = true;
  app.draw(app.card({ work: { ...work, count: 5, tools: tools.slice(0, 2), more: 3 }, messageId: 8 }), 'en');
  await waitFor(() => q('agora-work-toggle'), 'the failing card');
  q('agora-work-toggle').click();
  await waitFor(() => q('agora-work-list') && /3 more tools are not shown/.test(flat(q('agora-work-list'))), 'the notice');
  assert.equal(all('agora-work-tool').length, 2);

  // Shapes an older or newer hub could send never break a message.
  for (const none of [null, undefined, { count: 0, tools: [] }, { count: 'x' }]) {
    app.draw(app.card({ work: none, messageId: 9 }), 'en');
    await new Promise((r) => setTimeout(r, 5));
    assert.equal(q('agora-work'), null, 'no work, nothing drawn');
  }
  app.draw(app.card({ work: { count: 2, tools: [{ name: 'Unknown tool', ok: null, ms: null, summary: '' }, { name: 'X' }], more: 0, failed: 0, duration_s: 0, source: 'declared' }, messageId: 10 }), 'en');
  await waitFor(() => q('agora-work'), 'a sparse work');
  assert.equal(flat(q('agora-work-line')), 'Used 2 tools', 'no duration, no dot');

  // The routine behind a message.
  app.draw(app.routine({ id: 'resumen', name: 'Resumen diario', schedule: 'cada día a las 8:00' }), 'en');
  await waitFor(() => q('agora-routine'), 'the routine tag');
  assert.equal(flat(q('agora-routine')), 'Routine “Resumen diario”');
  assert.equal(q('agora-routine').title, 'cada día a las 8:00');
  app.draw(app.routine({ id: 'resumen', name: 'Resumen diario', schedule: '' }), 'es');
  await waitFor(() => q('agora-routine'), 'the Spanish routine tag');
  assert.equal(flat(q('agora-routine')), 'Rutina «Resumen diario»');
  assert.equal(q('agora-routine').getAttribute('title'), null);
  for (const none of [null, undefined, { id: 'x' }, { id: 'x', name: '' }]) {
    app.draw(app.routine(none), 'en');
    await new Promise((r) => setTimeout(r, 5));
    assert.equal(q('agora-routine'), null);
  }

  // Steps on the card of a task in progress: calls made, a bar of those finished and the minutes since the turn began.
  const now = 2_000_000_000_000;
  app.draw(app.steps({ steps: 5, done: 3, since: now / 1000 - 125, elapsed_s: 125, phase: 'act', tool: 'Bash' }, now), 'en');
  await waitFor(() => q('agora-live-steps'), 'the live steps');
  assert.equal(flat(q('agora-live-steps')), '5 steps · 2 min');
  const bar = q('agora-live-steps').querySelector('[role="progressbar"]');
  assert.deepEqual([bar.getAttribute('aria-valuenow'), bar.getAttribute('aria-valuemax')], ['3', '5']);
  assert.equal(bar.firstElementChild.style.inlineSize, '60%');
  assert.equal(q('agora-live-steps').title, 'Using Bash');
  app.draw(app.steps({ steps: 1, done: 0, since: now / 1000 - 20, elapsed_s: 20 }, now), 'es');
  await waitFor(() => q('agora-live-steps'), 'the Spanish live steps');
  assert.equal(flat(q('agora-live-steps')), '1 paso · 20 s');
  app.draw(app.steps({ steps: 4, done: 9, since: null, elapsed_s: null }, now), 'es');
  await waitFor(() => q('agora-live-steps'), 'a live without a start');
  assert.equal(flat(q('agora-live-steps')), '4 pasos · menos de un minuto');
  assert.equal(q('agora-live-steps').querySelector('[role="progressbar"]').getAttribute('aria-valuenow'), '4', 'finished never exceeds the calls made');
  for (const none of [null, undefined, { steps: 0, done: 0, since: 1, elapsed_s: 1 }]) {
    app.draw(app.steps(none, now), 'en');
    await new Promise((r) => setTimeout(r, 5));
    assert.equal(q('agora-live-steps'), null, 'before the first call, nothing');
  }
  assert.equal(app.span(0), '');
  assert.equal(app.span(3700), '1 h 2 min');

  app.unmount();
  console.log('Agora work: tools under a message (folded, opened, long, failing, sparse), routine tag and live steps, EN/ES, passed.');
} finally {
  window.happyDOM.abort();
  rmSync(temporary, { recursive: true, force: true });
}
