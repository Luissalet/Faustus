#!/usr/bin/env node
/**
 * The /workflows screen's model-driven nodes, checked by behaviour: the real
 * WorkflowsScreen is mounted (esbuild + happy-dom) against a fake server and
 * driven the way a person would drive it — start a blank workflow, add nodes
 * from the palette, fill the per-type forms, gate a node on a branch, save to
 * the library, publish it as a tool, run an evaluation, open a run and read
 * its per-node state and per-pass loop table.
 *
 * Run: `node studio/checks/workflows-model-nodes.render.check.mjs`
 */
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

// ── the fake server ─────────────────────────────────────────────────────
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const RUN_DEFINITION = {
  id: 'triage', version: '1.0.0', title: 'Triage',
  nodes: [
    { id: 'start', type: 'manual', title: 'Start', needs: [], config: {} },
    { id: 'route', type: 'classify', title: 'Route', needs: ['start'], config: { text: 'x', labels: ['refund', 'other'], fallback: 'other' } },
    { id: 'refund', type: 'agent', title: 'Refund', needs: ['route'], branch: { route: ['refund'] }, config: { prompt: 'p' } },
    { id: 'rest', type: 'agent', title: 'Rest', needs: ['route'], branch: { route: ['other'] }, config: { prompt: 'p' } },
    { id: 'retry', type: 'loop', title: 'Retry', needs: ['refund'], config: { body: ['check'], budget: { max_iterations: 3 } } },
    { id: 'check', type: 'guard', title: 'Check', needs: [], config: { text: 'x', checks: ['secrets'] } },
  ],
};

const RUN_DETAIL = {
  ok: true,
  run: { status: 'completed', reason: '' },
  definition: RUN_DEFINITION,
  nodes: {
    start: { status: 'completed', result: {} },
    route: { status: 'completed', result: { branch: 'refund' } },
    refund: { status: 'completed', result: {} },
    rest: { status: 'skipped', result: {}, reason: 'branch not taken' },
    retry: { status: 'completed', result: {} },
  },
  loops: {
    retry: [
      { iteration: 1, status: 'completed', reason: '', until: { passed: false }, nodes: { check: { status: 'failed', attempt: 1, reason: 'secrets found' } }, budget_used: { seconds: 1.5 } },
      { iteration: 2, status: 'completed', reason: '', until: { passed: true }, nodes: { check: { status: 'completed', attempt: 1, reason: '' } }, budget_used: { seconds: 0.5 } },
    ],
  },
};

function makeServer(calls) {
  const state = { library: {}, sets: {}, reports: [] };
  const rowOf = (name) => {
    const entry = state.library[name];
    const inputs = entry.definition.inputs && Object.keys(entry.definition.inputs).length > 0;
    return { name, tool: `wf_${name}`, title: entry.definition.title, description: '', workflow_id: entry.definition.id, version: '1.0.0',
      enabled: entry.enabled, allow_overrides: entry.allow_overrides, publishable: !!inputs, nodes: entry.definition.nodes.length, updated_at: 'now' };
  };
  return async (url, init = {}) => {
    const u = String(url);
    const method = (init.method || 'GET').toUpperCase();
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ url: u, method, body });
    if (u === '/api/agent-profiles') return json({ agents: [{ slug: 'reviewer' }, { slug: 'writer' }] });
    if (u === '/api/workflows/preflight') return json({ ok: true, preflight: { connections: [], warnings: [], human_waits: [] } });
    if (u === '/api/workflows/runs/run1') return json(RUN_DETAIL);
    if (u.startsWith('/api/workflows/runs?')) return json({ ok: true, runs: [] });
    if (u === '/api/workflows/templates') {
      return json({ ok: true, templates: [{ id: 'folder-pdf', title: 'Folder to PDF', description: 'Process new PDFs.', category: 'files', notes: ['Needs approval for the agent turn.'],
        parameters: [{ name: 'watch_dir', label: 'Watched folder', kind: 'folder', help: '', required: true, default: null }, { name: 'poll', label: 'Seconds', kind: 'integer', required: false, default: 30, minimum: 5 }],
        nodes: [{ id: 'watch', type: 'wait_for_event' }] }] });
    }
    if (u === '/api/workflows/templates/folder-pdf/instantiate') {
      if (!body.parameters.watch_dir) return json({ detail: 'watch_dir is required' }, 400);
      return json({ ok: true, definition: { id: 'folder-pdf', version: '1.0.0', title: 'Folder to PDF', nodes: [
        { id: 'watch', type: 'wait_for_event', title: 'Watch', needs: [], config: { folder: body.parameters.watch_dir } },
        { id: 'ops', type: 'agent', title: 'PDF operations', needs: ['watch'], config: { prompt: 'p', tools: ['pdf_ops'] } }] } });
    }
    if (u === '/api/workflows/library' && method === 'GET') return json({ ok: true, workflows: Object.keys(state.library).map(rowOf) });
    if (u === '/api/workflows/library' && method === 'POST') {
      const name = body.name || body.definition.id;
      state.library[name] = { definition: body.definition, enabled: false, allow_overrides: true };
      return json({ ok: true, workflow: rowOf(name) });
    }
    let m = /^\/api\/workflows\/library\/([^/]+)$/.exec(u);
    if (m) {
      const name = decodeURIComponent(m[1]);
      if (method === 'GET') return json({ ok: true, workflow: { ...rowOf(name), definition: state.library[name].definition } });
      if (method === 'PATCH') {
        if ('enabled' in body) state.library[name].enabled = body.enabled;
        if ('allow_overrides' in body) state.library[name].allow_overrides = body.allow_overrides;
        return json({ ok: true, workflow: rowOf(name) });
      }
      if (method === 'DELETE') { delete state.library[name]; return json({ ok: true }); }
    }
    if (u === '/api/workflows/published') {
      const tools = Object.keys(state.library).filter((n) => state.library[n].enabled).map((n) => ({
        name: `wf_${n}`, description: `Workflow ${n}`,
        inputSchema: { type: 'object', properties: { ...state.library[n].definition.inputs.properties, overrides: { type: 'object', description: 'per-run overrides', properties: { route: { type: 'object' } } }, wait_seconds: { type: 'number' } } } }));
      return json({ ok: true, tools });
    }
    m = /^\/api\/workflows\/library\/([^/]+)\/eval-sets$/.exec(u);
    if (m) return json({ ok: true, model_judge: false, sets: Object.entries(state.sets).map(([name, s]) => ({ name, cases: s.cases.length, scorers: s.scorers.length })) });
    m = /^\/api\/workflows\/library\/([^/]+)\/eval-sets\/([^/]+)$/.exec(u);
    if (m) {
      const set = decodeURIComponent(m[2]);
      if (method === 'PUT') { state.sets[set] = body; return json({ ok: true }); }
      if (method === 'GET') return json({ ok: true, set: { set: state.sets[set] } });
      if (method === 'DELETE') { delete state.sets[set]; return json({ ok: true }); }
    }
    m = /^\/api\/workflows\/library\/([^/]+)\/evaluate$/.exec(u);
    if (m) {
      const set = state.sets[body.set];
      const report = { id: `rep${state.reports.length + 1}`, set: body.set, status: 'completed', started_at: 't0', ended_at: 't1',
        summary: { mode: body.mode, total: set.cases.length, passed: 1, failed: 1, error: 0, pass_rate: 0.5, duration_ms_total: 20, duration_ms_avg: 10, duration_ms_p95: 12, unscored: 0, note: '', judge_enabled: false, by_scorer: { exact: { passed: 1, total: 2 } } },
        cases: [
          { id: 'a', name: '', status: 'passed', run_status: 'completed', run_id: 'r1', output: { reply: 'ok' }, error: '', note: '', duration_ms: 9, scores: [{ id: 's1', type: 'exact', path: 'reply', passed: true, detail: '' }] },
          { id: 'b', name: '', status: 'failed', run_status: 'completed', run_id: 'r2', output: { reply: 'no' }, error: '', note: '', duration_ms: 11, scores: [{ id: 's1', type: 'exact', path: 'reply', passed: false, detail: 'expected "ok", got "no"' }] },
        ] };
      state.reports.unshift(report);
      return json({ ok: true, finished: true, report });
    }
    if (/^\/api\/workflows\/library\/[^/]+\/evaluations$/.test(u)) return json({ ok: true, reports: state.reports });
    return json({ detail: `unhandled in fixture: ${method} ${u}` }, 404);
  };
}

// ── DOM helpers ─────────────────────────────────────────────────────────
async function main() {
  const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
  const harness = `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { MemoryRouter } from 'react-router';
    import { WorkflowsScreen } from './studio/src/screens/workflows/WorkflowsScreen';
    export function mount(id, entry) {
      createRoot(document.getElementById(id)).render(
        React.createElement(MemoryRouter, { initialEntries: [entry] }, React.createElement(WorkflowsScreen)));
    }`;
  const bundle = await build({
    stdin: { contents: harness, resolveDir: root, loader: 'tsx', sourcefile: 'harness.tsx' },
    bundle: true, format: 'esm', platform: 'browser', write: false, logLevel: 'silent',
    define: { 'process.env.NODE_ENV': '"production"' },
    loader: { '.tsx': 'tsx', '.ts': 'ts', '.css': 'empty' },
  });
  const tmp = mkdtempSync(join(tmpdir(), 'wf-model-nodes-'));
  const bundlePath = join(tmp, 'harness.mjs');
  writeFileSync(bundlePath, bundle.outputFiles[0].text, 'utf8');

  const window = new Window({ url: 'https://studio.faustus.invalid/' });
  const document = window.document;
  document.body.innerHTML = '<div id="a"></div><div id="b"></div><div id="fs-overlay-root"></div>';
  for (const key of ['window', 'document', 'HTMLElement', 'SVGElement', 'Node', 'NodeFilter', 'Element', 'Event', 'CustomEvent',
    'MouseEvent', 'localStorage', 'sessionStorage', 'MutationObserver', 'getComputedStyle', 'DocumentFragment',
    'requestAnimationFrame', 'cancelAnimationFrame', 'HTMLInputElement', 'HTMLSelectElement', 'HTMLTextAreaElement',
    'HTMLAnchorElement', 'HTMLButtonElement', 'FocusEvent', 'KeyboardEvent', 'PointerEvent', 'Blob', 'URL']) {
    if (key in window) { try { globalThis[key] = window[key]; } catch { /* getter-only */ } }
  }
  const calls = [];
  globalThis.fetch = makeServer(calls);
  const mod = await import(pathToFileURL(bundlePath).toString());

  const q = (id) => document.querySelector(`[data-testid="${id}"]`);
  const all = (prefix) => [...document.querySelectorAll(`[data-testid^="${prefix}"]`)];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function waitFor(probe, what, ms = 4000) {
    const until = Date.now() + ms;
    while (!probe() && Date.now() < until) await sleep(15);
    assert.ok(probe(), `timed out waiting for ${what}`);
  }
  const click = async (id) => { const el = typeof id === 'string' ? q(id) : id; assert.ok(el, `no element ${id}`); if (typeof el.click === 'function') el.click(); else el.dispatchEvent(new window.MouseEvent('click', { bubbles: true })); await sleep(20); };
  async function type(id, value) {
    const el = q(id);
    assert.ok(el, `no field ${id}`);
    const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : el.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
    el.dispatchEvent(new window.Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
    await sleep(20);
  }
  const text = (id) => (q(id)?.textContent ?? '');
  // an edge label is drawn shortened when long, with the whole text in its <title>
  const edgeText = (el) => el?.querySelector('title')?.textContent ?? '';

  mod.mount('a', '/workflows');
  await waitFor(() => q('workflows-screen') && q('build-panel'), 'the screen and its build panel');

  // 1. blank workflow, palette, classify form, branch labels on edges
  await click('build-new');
  await waitFor(() => q('plan-node-start'), 'the blank workflow to draw');
  await click('palette-classify');
  await waitFor(() => q('form-classify'), 'the classify form to open for the new node');
  assert.ok(q('plan-node-classify'), 'the classify node is on the graph');
  await type('form-classify-text', '{{ inputs.ticket }}');
  await type('form-classify-labels', 'billing: invoices\nbug\nother');
  await type('form-classify-fallback', 'other');
  await click('node-inspector-apply');
  await waitFor(() => calls.some((c) => c.url === '/api/workflows/preflight' && JSON.stringify(c.body).includes('"billing"')), 'preflight to re-run with the new labels');
  const applied = calls.filter((c) => c.url === '/api/workflows/preflight').pop().body.definition.nodes.find((n) => n.id === 'classify');
  assert.deepEqual(applied.config.labels, [{ name: 'billing', description: 'invoices' }, 'bug', 'other'], 'labels keep descriptions and plain names');
  assert.equal(applied.config.fallback, 'other');
  assert.equal(applied.config.threshold, 0.7);

  // a node after the classify is gated on two of its options
  await click('palette-agent');
  await waitFor(() => q('form-agent'), 'the agent form');
  assert.ok(q('node-inspector-branch'), 'the agent may be gated on the classify options');
  await click('node-inspector-branch-classify-billing');
  await click('node-inspector-branch-classify-other');
  await type('form-agent-prompt', 'Reply to {{ inputs.ticket }}');
  await type('form-agent-tools-mode', 'none');
  await type('form-agent-output-schema', '{"type":"object"}');
  await click('node-inspector-apply');
  await waitFor(() => q('plan-edge-label-classify-agent'), 'the branch label on the edge');
  assert.equal(edgeText(q('plan-edge-label-classify-agent')), 'billing / other (uncertain)', 'the fallback option reads as the uncertain route');
  const agentNode = calls.filter((c) => c.url === '/api/workflows/preflight').pop().body.definition.nodes.find((n) => n.id === 'agent');
  assert.deepEqual(agentNode.branch, { classify: ['billing', 'other'] });
  assert.deepEqual(agentNode.config.tools, [], 'none means think-only, not the default');
  assert.deepEqual(agentNode.config.output_schema, { type: 'object' });
  assert.equal(agentNode.config.prompt, 'Reply to {{ inputs.ticket }}');

  // 2. guard: checks, model question, pass/fail labels
  await click('plan-node-classify');
  await click('palette-guard');
  await waitFor(() => q('form-guard'), 'the guard form');
  await type('form-guard-text', '{{ results.agent.text }}');
  await click('form-guard-add-model');
  await type('form-guard-model-question-1', 'Does the text promise a refund?');
  await click('form-guard-add-urls');
  await type('form-guard-urls-deny-2', 'evil.example\nbad.example');
  await click('node-inspector-apply');
  await waitFor(() => calls.filter((c) => c.url === '/api/workflows/preflight').pop().body.definition.nodes.some((n) => n.id === 'guard' && n.config.checks?.length === 3), 'the guard checks to be applied');
  const guard = calls.filter((c) => c.url === '/api/workflows/preflight').pop().body.definition.nodes.find((n) => n.id === 'guard');
  assert.deepEqual(guard.config.checks[0], 'secrets');
  assert.equal(guard.config.checks[1].type, 'model');
  assert.deepEqual(guard.config.checks[2].deny, ['evil.example', 'bad.example']);

  // 3. loop: palette adds one body agent; the form lists what can be repeated
  await click('palette-loop');
  await waitFor(() => q('form-loop'), 'the loop form');
  assert.ok(q('plan-node-loop_step'), 'a loop starts with one body node');
  assert.equal(q('plan-node-loop_step').getAttribute('data-loop-body'), 'loop', 'the body node is drawn as owned by the loop');
  assert.equal(edgeText(q('plan-edge-label-loop-loop_step')), 'each pass');
  await type('form-loop-max-iterations', '4');
  await click('form-loop-until-on');
  await type('form-loop-until-left', 'results.loop_step.ok');
  await click('node-inspector-apply');
  await waitFor(() => { const d = calls.filter((c) => c.url === '/api/workflows/preflight').pop().body.definition; return d.nodes.find((n) => n.id === 'loop')?.config.budget.max_iterations === 4; }, 'the loop ceiling to be applied');
  const loop = calls.filter((c) => c.url === '/api/workflows/preflight').pop().body.definition.nodes.find((n) => n.id === 'loop');
  assert.deepEqual(loop.config.body, ['loop_step']);
  assert.deepEqual(loop.config.until, { left: 'results.loop_step.ok', op: 'truthy' });
  assert.equal(loop.config.budget.on_exhausted, 'pause', 'pausing for a person is the default when a limit is hit');

  // 4. raw JSON stays in step with the form
  await click('plan-node-classify');
  await waitFor(() => q('form-classify'), 'classify selected again');
  assert.ok(q('node-inspector-config').value.includes('"threshold": 0.7'), 'the raw JSON shows what the form wrote');
  await type('node-inspector-config', '{ not json');
  assert.ok(document.body.textContent.includes('config must be valid JSON.'), 'bad JSON is reported in place');
  assert.ok(q('node-inspector-apply').disabled, 'and cannot be applied');

  // 5. removing a node removes what pointed at it
  await click('plan-node-agent');
  await click('node-inspector-remove');
  await waitFor(() => !q('plan-node-agent'), 'the agent to disappear');
  assert.equal(q('plan-edge-label-classify-agent'), null, 'its edge label goes with it');

  // 6. library: declare inputs, save, publish, read the tool details
  await type('library-inputs', '{"type":"object","properties":{"ticket":{"type":"string"}},"required":["ticket"]}');
  await type('library-name', 'triage');
  await click('library-save');
  await waitFor(() => q('library-row-triage'), 'the saved workflow to be listed');
  assert.equal(text('library-state-triage'), 'Not published', 'saving never publishes');
  assert.equal(q('library-publish-triage').disabled, false, 'it declares inputs, so it may be published');
  await click('library-publish-triage');
  await waitFor(() => text('library-state-triage') === 'Published', 'the tool to be published');
  await click('library-details-triage');
  await waitFor(() => q('library-tool-name-triage'), 'the tool details');
  assert.equal(text('library-tool-name-triage'), 'wf_triage');
  assert.ok(text('library-tool-schema-triage').includes('"ticket"') && text('library-tool-schema-triage').includes('"overrides"'), 'the input schema shows the workflow inputs and the overrides argument');
  assert.ok(text('library-tool-overrides-triage').includes('"route"'), 'the overrides schema is shown');
  const snippet = text('library-client-config');
  assert.ok(snippet.includes('workflows_server.py') && snippet.includes('FAUSTUS_API_TOKEN') && snippet.includes('agents:dispatch'), 'the client configuration names the server and the token scope');
  assert.ok(!/(^|[^A-Za-z])[A-Za-z]:[\\/]/.test(snippet), 'and contains no machine-specific path');

  // 7. evaluate: a saved set, simulate by default, real needs a second step
  await click('workflows-mode-evaluate');
  await waitFor(() => q('eval-panel') && q('eval-new-set'), 'the evaluate panel');
  await click('eval-new-set');
  await waitFor(() => q('eval-editor'), 'the set editor');
  await click('eval-add-scorer');
  await type('eval-scorer-path-0', 'reply');
  await type('eval-scorer-expected-0', 'ok');
  await type('eval-case-inputs-0', '{"ticket":"refund please"}');
  await click('eval-add-case');
  await type('eval-case-inputs-1', '{"ticket":"other"}');
  assert.ok(q('eval-run').disabled, 'an unsaved set cannot be run');
  await click('eval-save-set');
  await waitFor(() => q('eval-set-set-1'), 'the set to be listed');
  assert.ok(!q('eval-run').disabled, 'a saved set can be run in simulate mode');
  assert.equal(q('eval-mode-simulate').checked, true, 'simulate is the default');
  await click('eval-mode-real');
  assert.ok(q('eval-run').disabled, 'real mode is refused until the box is ticked');
  await click('eval-allow-real');
  assert.ok(!q('eval-run').disabled);
  await click('eval-mode-simulate');
  assert.equal(q('eval-allow-real'), null, 'going back to simulate drops the real-run confirmation');
  await click('eval-run');
  await waitFor(() => q('eval-report'), 'the report');
  const evalCall = calls.filter((c) => c.url.endsWith('/evaluate')).pop();
  assert.equal(evalCall.body.mode, 'simulate');
  assert.equal(evalCall.body.allow_real, undefined, 'simulate never sends the real-run flag');
  assert.equal(text('eval-pass-rate'), '50%');
  assert.equal(text('eval-mode'), 'simulated', 'the report says it was simulated');
  assert.ok(text('eval-case-b').includes('expected "ok", got "no"'), 'a failed case shows what the scorer saw');

  // 8. templates: parameters, server refusal shown, then a definition
  await click('workflows-mode-design');
  await waitFor(() => q('build-template-select'), 'the template picker');
  await type('build-template-select', 'folder-pdf');
  await waitFor(() => q('build-param-watch_dir'), 'the template parameters');
  await click('build-template-fill');
  await waitFor(() => q('build-template-error'), 'the server refusal');
  assert.ok(text('build-template-error').includes('watch_dir is required'));
  await type('build-param-watch_dir', 'incoming');
  await click('build-template-fill');
  await waitFor(() => q('plan-node-ops'), 'the template definition to draw');
  assert.ok(q('plan-node-watch'));

  // 9. a finished run: which way it went, and what each loop pass did
  mod.mount('b', '/workflows?run=run1');
  await waitFor(() => document.querySelector('#b [data-testid="run-overlay"]'), 'the run overlay', 6000);
  const inB = (id) => document.querySelector(`#b [data-testid="${id}"]`);
  await waitFor(() => inB('plan-edge-route-refund'), 'the run graph');
  assert.equal(inB('plan-edge-route-refund').querySelector('path').getAttribute('data-mark'), 'activated', 'the edge the run took is marked');
  assert.equal(inB('plan-edge-route-rest').querySelector('path').getAttribute('data-mark'), 'not_taken', 'the edge it did not take is marked');
  assert.equal(edgeText(inB('plan-edge-label-route-refund')), 'refund');
  assert.equal(edgeText(inB('plan-edge-label-route-rest')), 'other (uncertain)');
  assert.ok(inB('run-branches').textContent.includes('route went to refund'));
  assert.equal(inB('plan-badge-retry').textContent, '2 pass(es)');
  assert.ok(inB('run-loop-retry'), 'the loop has a per-pass table');
  assert.ok(inB('run-loop-retry-pass-1').textContent.includes('secrets found'), 'pass 1 shows why its step failed');
  assert.ok(inB('run-loop-retry-pass-1').textContent.includes('not met'));
  assert.ok(inB('run-loop-retry-pass-2').textContent.includes('met'));
  assert.equal(inB('plan-node-check').getAttribute('data-mark'), 'run_completed', 'a body node takes the colour of its last pass');

  rmSync(tmp, { recursive: true, force: true });
  console.log(`ok workflows-model-nodes (${calls.length} fetch calls)`);
  process.exit(0);
}

main().catch((err) => { console.error(err); process.exit(1); });
