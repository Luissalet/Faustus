/** The agent loop panel: practices chosen by the kind of work, readable steps and the ring, in both languages. */
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const temporary = mkdtempSync(join(tmpdir(), 'agent-loop-'));
const window = new Window({ url: 'https://studio.faustus.invalid/' });
for (const key of ['window', 'document', 'HTMLElement', 'SVGElement', 'Node', 'Element', 'Event', 'localStorage',
  'MutationObserver', 'getComputedStyle', 'DocumentFragment', 'requestAnimationFrame', 'cancelAnimationFrame']) {
  if (key in window) globalThis[key] = window[key];
}
document.body.innerHTML = '<div id="root"></div>';
const q = (id) => document.querySelector(`[data-testid="${id}"]`);
const all = (sel) => [...document.querySelectorAll(sel)];

const step = (tool, command, state = 'succeeded', round = 1, durationMs = 500, output = '') =>
  ({ id: `${tool}-${round}-${command.length}`, tool, label: tool, state, command, round, durationMs, output });
const turn = (steps, text, extra = {}) => ({
  id: 't', role: 'assistant', text, thinking: '', thoughts: [], steps, rounds: steps.length ? Math.max(...steps.map((s) => s.round)) : 0,
  sources: [], streaming: false, images: [], checks: [], workers: [], ...extra,
});

try {
  const outfile = join(temporary, 'check.mjs');
  await build({ stdin: { contents: `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { flushSync } from 'react-dom';
    import { AgentLoopPanel } from './studio/src/components/agentloop/AgentLoopPanel';
    import { AgentLoopRing, nextAngle } from './studio/src/components/agentloop/AgentLoopRing';
    export { classify, judgePractices, argsSummary, toolTitle, loopOfTurn, language } from './studio/src/components/agentloop/loop';
    import { setLang } from './studio/src/i18n';
    export { nextAngle };
    const root = createRoot(document.getElementById('root'));
    let version = 0;
    export function panel(view, lang, live = false) {
      setLang(lang, { persist: false });
      flushSync(() => root.render(<AgentLoopPanel key={++version} view={view} live={live} />));
    }
    export function ring(phase, lap, lang) {
      setLang(lang, { persist: false });
      flushSync(() => root.render(<AgentLoopRing key={++version} phase={phase} lap={lap} agent="Faustus" />));
    }
    // Successive phases on one mounted ring, as a live turn feeds it: the angle each step leaves.
    export function travel(steps) {
      setLang('en', { persist: false });
      const key = ++version;
      return steps.map(([phase, lap]) => {
        flushSync(() => root.render(<AgentLoopRing key={key} phase={phase} lap={lap} />));
        const style = document.querySelector('.fs-agent-loop-ring__comet')?.getAttribute('style') ?? '';
        return parseFloat(style.split('rotate(')[1] ?? 'NaN');
      });
    }
    export function unmount() { root.unmount(); }
  `, resolveDir: root, loader: 'tsx' }, bundle: true, format: 'esm', platform: 'browser', outfile,
    jsx: 'automatic', loader: { '.css': 'empty' }, define: { 'process.env.NODE_ENV': '"production"' }, logLevel: 'silent' });
  const app = await import(pathToFileURL(outfile));

  // What a turn did decides its checklist.
  const call = (tool, command, ok = true) => ({ tool, command, ok });
  assert.equal(app.classify([]), 'chat');
  assert.equal(app.classify([call('read_file', '{"path":"src/x.py"}')]), 'chat');
  assert.equal(app.classify([call('web_search', '{"query":"bitcoin"}')]), 'research');
  assert.equal(app.classify([call('web_search', '{}'), call('edit_file', '{"path":"src/a.ts"}')]), 'code');
  assert.equal(app.classify([call('write_file', '{"path":"out/informe.docx"}')]), 'files');
  assert.equal(app.classify([call('bash', 'docker restart web')]), 'ops');
  assert.equal(app.classify([call('bash', 'git status')]), 'code');
  assert.equal(app.language('busca el precio del bitcoin'), 'es');
  assert.equal(app.argsSummary('{"query":"precio del bitcoin hoy","max_results":5}'), 'precio del bitcoin hoy');
  assert.equal(app.toolTitle('mcp__playwright__browser_navigate'), 'Browser · navigate');

  // A web search turn: research practices only, readable steps, in Spanish.
  const research = turn([
    step('web_search', '{"query":"precio del bitcoin hoy"}', 'succeeded', 1, 820, '1. https://www.coindesk.com/price/bitcoin'),
    step('web_fetch', '{"url":"https://www.coindesk.com/price/bitcoin"}', 'failed', 2, 1300),
  ], 'El bitcoin cotiza a 91.000 $ el 17 de noviembre de 2026 según [CoinDesk](https://www.coindesk.com/price/bitcoin).',
  { sources: [{ title: 'CoinDesk', url: 'https://www.coindesk.com/price/bitcoin' }] });
  const view = app.loopOfTurn(research, undefined, Date.now(), true, '¿Cuál es el precio del bitcoin hoy?');
  assert.equal(view.kind, 'research');
  const marks = Object.fromEntries(view.practices.map((p) => [p.id, p.state]));
  assert.deepEqual(marks, { sources_opened: 'pending', sources_cited: 'ok', dated: 'ok', verified: 'ok', language: 'ok' });
  app.panel(view, 'es');
  assert.equal(q('agent-loop-kind').textContent, 'Investigación');
  const practiceIds = all('[data-practice]').map((li) => li.getAttribute('data-practice'));
  assert.deepEqual(practiceIds, ['sources_opened', 'sources_cited', 'dated', 'verified', 'language']);
  assert.ok(!document.body.textContent.includes('Tests ejecutados'), 'no code practices on a research turn');
  const summary = document.querySelector('.fs-agent-loop__score').textContent;
  assert.equal(summary, '4 cumplidas · 0 incumplidas · 1 faltan');
  const tools = all('[data-testid="agent-loop-steps"] li[data-kind="tool"]');
  assert.equal(tools[0].querySelector('strong').textContent, 'Buscar en la web');
  assert.equal(tools[0].querySelector('.fs-agent-loop__detail').textContent, 'precio del bitcoin hoy');
  assert.ok(!q('agent-loop-steps').textContent.includes('{'), 'no raw JSON in the steps');
  const results = all('[data-testid="agent-loop-steps"] li[data-kind="result"]');
  assert.equal(results[0].querySelector('.fs-agent-loop__verdict').textContent, '✓ ok');
  assert.match(results[0].textContent, /0\.8s/);
  assert.equal(results[1].querySelector('.fs-agent-loop__verdict').textContent, '✕ error');
  assert.match(results[1].textContent, /1\.3s/);

  // A plain conversation has no checklist at all.
  const chat = app.loopOfTurn(turn([], '¡Hola! Todo bien.'), undefined, Date.now(), true, 'hola, ¿qué tal?');
  assert.equal(chat.kind, 'chat');
  app.panel(chat, 'es');
  assert.equal(q('agent-loop-practices'), null);
  assert.equal(document.querySelector('.fs-agent-loop__score'), null);
  assert.equal(q('agent-loop-kind').textContent, 'Conversación');

  // A code turn keeps the code checklist.
  const code = app.loopOfTurn(turn([
    step('edit_file', '{"path":"src/x.py","old":"a","new":"b"}'),
    step('bash', 'python -m pytest -q'),
    step('bash', 'git commit --author="Luissalet <l@x>" -m "Fix x"'),
  ], 'Done.'), undefined, Date.now(), true, 'fix the bug in x');
  assert.equal(code.kind, 'code');
  assert.deepEqual(code.practices.map((p) => p.id), ['tests', 'browser', 'docs', 'commit', 'no_evidence', 'agora']);
  app.panel(code, 'en');
  assert.equal(q('agent-loop-kind').textContent, 'Code');
  assert.equal(all('[data-testid="agent-loop-steps"] li[data-kind="tool"] strong')[0].textContent, 'Edit');

  // Files: the format asked for is judged; operations: checked after running.
  const files = app.judgePractices([call('write_file', '{"path":"out/informe.md"}'), call('read_file', '{"path":"out/informe.md"}')],
    { prompt: 'Hazme un informe en PDF con las ventas', answer: 'Aquí tienes el informe.' });
  assert.deepEqual(Object.fromEntries(files.map((p) => [p.id, p.state])), { output_checked: 'ok', format: 'fail', language: 'ok' });
  const ops = app.judgePractices([call('bash', 'docker restart web'), call('bash', 'docker ps')], { prompt: 'reinicia el contenedor web', answer: 'Hecho, está en marcha.' });
  assert.deepEqual(Object.fromEntries(ops.map((p) => [p.id, p.state])), { ran_ok: 'ok', checked: 'ok', language: 'ok' });

  // The ring: its lit arc only moves forward, labels are pills outside the ring, the lap and agent in the centre.
  assert.deepEqual([app.nextAngle(null, 'plan'), app.nextAngle(-90, 'act'), app.nextAngle(30, 'observe'), app.nextAngle(150, 'plan'), app.nextAngle(150, 'act')], [-90, 30, 150, 270, 390]);
  // The order a live turn really sends (the lap moves on at the start of a round): the arc never turns back.
  const angles = app.travel([['plan', 1], ['act', 1], ['observe', 1], ['observe', 2], ['act', 2], ['observe', 3], ['plan', 4], ['plan', 4]]);
  assert.deepEqual(angles, [-90, 30, 150, 150, 390, 510, 630, 630]);
  assert.ok(angles.every((a, i) => i === 0 || a >= angles[i - 1]), 'the lit arc only moves clockwise');
  app.ring('act', 3, 'es');
  const ringEl = q('agent-loop-ring');
  assert.deepEqual(all('.fs-agent-loop-ring__pill').map((p) => p.textContent), ['Planear', 'Actuar', 'Observar']);
  assert.equal(document.querySelector('.fs-agent-loop-ring__pill[data-active="true"]').textContent, 'Actuar');
  assert.equal(document.querySelector('.fs-agent-loop-ring__lap').textContent, '3');
  assert.equal(document.querySelector('.fs-agent-loop-ring__centre small').textContent, 'Vuelta');
  assert.match(document.querySelector('.fs-agent-loop-ring__agent').textContent, /Faustus/);
  assert.match(ringEl.querySelector('.fs-agent-loop-ring__comet').getAttribute('style'), /rotate\(30deg\)/);
  assert.deepEqual(all('.fs-agent-loop-ring__node').map((n) => n.getAttribute('data-state')), ['passed', 'active', 'ahead']);
  app.ring('done', 2, 'en');
  assert.equal(document.querySelector('.fs-agent-loop-ring__centre small').textContent, 'Done');
  assert.deepEqual(all('.fs-agent-loop-ring__pill').map((p) => p.textContent), ['Plan', 'Act', 'Observe']);
  app.unmount();
  console.log('Agent loop: kinds, practices, readable steps and ring OK');
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
