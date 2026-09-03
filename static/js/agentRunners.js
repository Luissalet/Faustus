// static/js/agentRunners.js
// Agent runners — the CLI agents this machine can run AS A WORKER.
//
// The backend is authoritative (src/agent_runners.py, routes/agent_runner_routes.py):
// the list comes from `ollama launch --help` parsed at runtime, merged with
// Faustus's own table of how to run ONE task with each of them. This module is
// the human end: one row per agent — label, licence word, installed or not, the
// launch command with a copy button, and, for the ones that really can be a
// worker, the dispatch field to put its key in.
//
// Three honesty rules run through this file and are not negotiable:
//
//   * the LICENCE WORD is printed verbatim — "open", "subscription" or
//     "unknown". The renderer never turns "unknown" into a guess, and never
//     derives a word from the agent's name;
//   * "installed" and "can be a worker" are two different facts and are shown
//     as two: VS Code is installed and can never be a worker; OpenCode knows
//     how to be one and is not installed;
//   * every payload here carries the one sentence this feature must not hide —
//     Faustus's command guard cannot see inside another agent's own shell —
//     and the page prints it above the table, not in a tooltip.
//
// The renderers are pure and live between the marked region below, so
// tests/test_agent_runners_page_js.py can run them in bare node.

const API = `${window.location.origin}/api/agent-runners`;

// ── Agent runners: pure helpers (dependency-free; extracted and run under node by tests) ──
// Everything between these markers must stay free of DOM, module and window
// references so tests/test_agent_runners_page_js.py can execute it in bare node.

/** Local escape: same table as ui.js esc(), but import-free for tests. */
function runEsc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

/** Sanitize a server word (licence, kind) for use in a class name. */
function runToken(value) {
  return String(value == null ? '' : value).toLowerCase().replace(/[^a-z0-9_-]/g, '');
}

/** The three licence words the backend may send. Anything else is unknown. */
const RUN_LICENCES = ['open', 'subscription', 'unknown'];

/**
 * The licence word, VERBATIM when the backend sent one of the three it is
 * allowed to send, and "unknown" otherwise.
 *
 * It never invents a word, and it never derives one from the agent's name: an
 * agent whose licence Faustus has not established says "unknown", which is the
 * honest answer and not a placeholder for a guess.
 */
function licenceWord(raw) {
  const word = String(raw == null ? '' : raw).trim().toLowerCase();
  return RUN_LICENCES.includes(word) ? word : 'unknown';
}

/** What each licence word means, for the title attribute. Never a claim beyond it. */
function licenceHint(word) {
  if (word === 'open') return 'Openly licensed: you can run it without buying an account.';
  if (word === 'subscription') return 'Needs a paid account with its vendor.';
  return 'Faustus has not established a licence for this one. It says so rather than guess.';
}

/** One catalogue row (src/agent_runners.to_row), with every field defaulted. */
function normalizeRunner(raw) {
  const row = raw && typeof raw === 'object' ? raw : {};
  const argv = Array.isArray(row.argv) ? row.argv.map(String) : [];
  return {
    key: String(row.key == null ? '' : row.key),
    label: String(row.label || row.key || ''),
    aliases: Array.isArray(row.aliases) ? row.aliases.map(String) : [],
    kind: row.kind === 'app' ? 'app' : 'cli',
    licence: licenceWord(row.licence),
    install: String(row.install || ''),
    launch_command: String(row.launch_command || row.install || ''),
    argv,
    installed: Boolean(row.installed),
    path: String(row.path || ''),
    version: String(row.version || ''),
    // Two separate facts, never collapsed into one.
    invocation_known: row.invocation_known === undefined ? argv.length > 0 : Boolean(row.invocation_known),
    runnable_as_worker: Boolean(row.runnable_as_worker),
    notes: String(row.notes || ''),
  };
}

/** GET /api/agent-runners: {"runners":[…]}, a bare list, or a {"data": …} wrapper. */
function normalizeCatalogue(raw) {
  let list = [];
  if (Array.isArray(raw)) list = raw;
  else if (raw && typeof raw === 'object') {
    if (Array.isArray(raw.runners)) list = raw.runners;
    else if (Array.isArray(raw.items)) list = raw.items;
    else if (raw.data && typeof raw.data === 'object' && Array.isArray(raw.data.runners)) list = raw.data.runners;
  }
  return list
    .filter(row => row && typeof row === 'object' && row.key != null && String(row.key))
    .map(normalizeRunner);
}

/** Installed first, then the ones that could be workers, then by label. */
function sortRunners(rows) {
  const all = (rows || []).map(normalizeRunner);
  return all.slice().sort((a, b) => {
    if (a.installed !== b.installed) return a.installed ? -1 : 1;
    if (a.runnable_as_worker !== b.runnable_as_worker) return a.runnable_as_worker ? -1 : 1;
    if (a.invocation_known !== b.invocation_known) return a.invocation_known ? -1 : 1;
    return a.label.localeCompare(b.label);
  });
}

function filterRunners(rows, query) {
  const all = (rows || []).map(normalizeRunner);
  const needle = String(query == null ? '' : query).trim().toLowerCase();
  if (!needle) return all;
  return all.filter(row => (
    row.key.toLowerCase().includes(needle)
    || row.label.toLowerCase().includes(needle)
    || row.aliases.some(alias => alias.toLowerCase().includes(needle))
  ));
}

/**
 * Why this agent can or cannot be a worker, in words — the two facts kept
 * apart. A GUI never can; a CLI with no recorded invocation cannot yet; a CLI
 * that is not installed says how to install it.
 */
function workerStatus(row) {
  const r = normalizeRunner(row);
  if (r.kind === 'app') {
    return { can: false, label: 'GUI, never a worker',
             detail: 'A window that stays open has no one-task, one-exit invocation.' };
  }
  if (!r.invocation_known) {
    return { can: false, label: 'no invocation recorded',
             detail: 'Ollama knows this agent; Faustus has no row saying how to run one task with it yet.' };
  }
  if (!r.installed) {
    return { can: false, label: 'not installed',
             detail: `Install it first: ${r.install || r.launch_command}` };
  }
  return { can: true, label: 'can be a worker',
           detail: `Put "runner": "${r.key}" on a dispatched task.` };
}

/** One table row. */
function runnerRowHtml(raw) {
  const row = normalizeRunner(raw);
  const worker = workerStatus(row);
  const aliases = row.aliases.length
    ? `<span class="run-aliases" title="Also known as">${runEsc(row.aliases.join(', '))}</span>` : '';
  const version = row.version ? `<span class="run-version">${runEsc(row.version)}</span>` : '';
  return `
    <tr class="run-row${row.installed ? ' is-installed' : ''}" data-run-key="${runEsc(row.key)}">
      <td class="run-cell-name">
        <span class="run-name">${runEsc(row.label)}</span>
        <code class="run-key">${runEsc(row.key)}</code>
        ${aliases}
        ${row.notes ? `<span class="run-notes">${runEsc(row.notes)}</span>` : ''}
      </td>
      <td class="run-cell-licence">
        <span class="run-licence is-${runToken(row.licence)}" title="${runEsc(licenceHint(row.licence))}">${runEsc(row.licence)}</span>
      </td>
      <td class="run-cell-installed">
        <span class="run-installed${row.installed ? ' is-yes' : ''}">${row.installed ? 'installed' : 'not installed'}</span>
        ${version}
      </td>
      <td class="run-cell-worker">
        <span class="run-worker${worker.can ? ' is-yes' : ''}">${runEsc(worker.label)}</span>
        <span class="run-worker-detail">${runEsc(worker.detail)}</span>
      </td>
      <td class="run-cell-launch">
        <code class="run-command">${runEsc(row.launch_command)}</code>
        <button type="button" class="run-btn run-copy" data-run-copy="${runEsc(row.launch_command)}"
                aria-label="Copy the launch command for ${runEsc(row.label)}">Copy</button>
        <button type="button" class="run-btn run-launch" data-run-launch="${runEsc(row.key)}"
                aria-label="Run the launch command for ${runEsc(row.label)}">Launch</button>
      </td>
    </tr>`;
}

/** The whole page body. */
function runnersPageHtml(payload, opts = {}) {
  const state = opts || {};
  const data = payload && typeof payload === 'object' ? payload : {};
  const all = normalizeCatalogue(data);
  const visible = sortRunners(filterRunners(all, state.query));
  const installed = all.filter(row => row.installed).length;
  const runnable = all.filter(row => row.runnable_as_worker).length;
  const rows = state.loading
    ? '<tr><td class="run-empty" colspan="5">Loading agent runners…</td></tr>'
    : (visible.map(runnerRowHtml).join('') || `<tr><td class="run-empty" colspan="5">${
      all.length ? 'No agent matches that search.'
        : 'No agent runners: this machine has no Ollama that knows any, and the built-in table could not be read.'}</td></tr>`);
  const off = data.enabled === false
    ? `<p class="run-note">External agent runners are <strong>off</strong> (<code>agent_external_runners</code>, Settings → Agent &amp; automation). It ships off because it runs third-party binaries on this machine. A dispatched task naming a <code>runner</code> is refused with that reason until you turn it on.</p>`
    : `<p class="run-note is-on">External agent runners are <strong>on</strong>: a dispatched task may name a <code>runner</code>, and that agent does the work.</p>`;
  const guard = String(data.guard_note || '');
  return `
    <div class="run-head">
      <div>
        <h2 class="run-title">Agent runners</h2>
        <p class="run-desc">Any of these can be one of Faustus's workers: the checkpoint before, the diff after, Faustus's own verification and the honest proof — around an agent Faustus did not write.</p>
      </div>
      <div class="run-toolbar">
        <input type="search" class="run-search" data-run-query placeholder="Search agents" aria-label="Search agent runners" value="${runEsc(state.query || '')}" spellcheck="false" />
        <button type="button" class="run-btn" data-run-refresh>Refresh</button>
      </div>
    </div>
    <p class="run-guard"><strong>What this cannot promise:</strong> ${runEsc(guard || 'an external agent runs its own shell; Faustus does not see the commands it runs.')} Every job that uses one says so in its verdict and carries <code>external_agent_unguarded</code> in its proof.</p>
    ${off}
    <p class="run-counts">${all.length} known · ${installed} installed · ${runnable} usable as a worker right now</p>
    <p class="run-error" data-run-error${state.error ? '' : ' hidden'}>${runEsc(state.error || '')}</p>
    <div class="run-table-wrap">
      <table class="run-table">
        <thead><tr><th>Agent</th><th>Licence</th><th>On this machine</th><th>As a worker</th><th>Install / launch</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <pre class="run-log" data-run-log${state.log ? '' : ' hidden'}>${runEsc(state.log || '')}</pre>`;
}

/** The lines of a launch stream, as text — used by the live log below. */
function launchLogLine(event) {
  const ev = event && typeof event === 'object' ? event : {};
  const kind = String(ev.event || '');
  if (kind === 'started') return `$ ${String(ev.command || '')}`;
  if (kind === 'output') return String(ev.line || '');
  if (kind === 'error') return `error: ${String(ev.message || '')}`;
  if (kind === 'end') {
    const code = ev.exit_code == null ? 'unknown' : String(ev.exit_code);
    return `— finished (exit ${code}); ${ev.installed ? 'it is now installed' : 'it is still not installed'}`;
  }
  return '';
}

// ── Agent runners: end pure helpers ──

export { runnersPageHtml, runnerRowHtml, normalizeCatalogue, sortRunners, filterRunners,
         licenceWord, workerStatus, launchLogLine };

const $ = (id) => document.getElementById(id);

const MODAL_ID = 'agent-runners-modal';
const BODY_ID = 'agent-runners-body';

let _payload = null;
let _state = { query: '', error: '', loading: false, log: '' };
let _wired = false;
let _loaded = false;
let _returnFocus = null;
let _stream = null;

/** fetch wrapper for /api/agent-runners/*: a non-2xx becomes an Error. */
async function req(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON body */ }
  if (!res.ok) {
    const detail = data && data.detail != null
      ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
      : '';
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}

export const listRunners = (refresh = false) => req(refresh ? '?refresh=1' : '');
export const readRunner = (key) => req(`/${encodeURIComponent(key)}`);

function render() {
  const host = $(BODY_ID);
  if (!host) return;
  host.innerHTML = runnersPageHtml(_payload, _state);
}

function inlineError(message) {
  _state.error = message || '';
  const slot = $(BODY_ID)?.querySelector('[data-run-error]');
  if (!slot) { render(); return; }
  slot.textContent = _state.error;
  slot.hidden = !_state.error;
}

export async function loadRunners(force = false) {
  if (_loaded && !force) return _payload;
  _state.loading = true;
  _state.error = '';
  render();
  try {
    _payload = await listRunners(force);
    _loaded = true;
  } catch (error) {
    _payload = null;
    _state.error = `Could not read the agent runners: ${error.message || error}`;
  } finally {
    _state.loading = false;
    render();
  }
  return _payload;
}

async function copyCommand(text) {
  try {
    await navigator.clipboard.writeText(String(text || ''));
    inlineError('');
  } catch (_) {
    inlineError('Could not copy — select the command and copy it by hand.');
  }
}

/**
 * Run `ollama launch <key>` and show its output as it arrives. This INSTALLS
 * software, so it is only ever a button the human pressed.
 */
async function launch(key) {
  if (_stream) { try { _stream.abort(); } catch (_) {} _stream = null; }
  _state.log = `$ ollama launch ${key}\n`;
  render();
  const controller = new AbortController();
  _stream = controller;
  try {
    const res = await fetch(`${API}/${encodeURIComponent(key)}/launch`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config_only: false }),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      let detail = `HTTP ${res.status}`;
      try { const data = await res.json(); if (data && data.detail) detail = data.detail; } catch (_) {}
      throw new Error(detail);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split('\n\n');
      buffer = frames.pop() || '';
      for (const frame of frames) {
        for (const line of frame.split('\n')) {
          if (!line.startsWith('data:')) continue;
          let payload = null;
          try { payload = JSON.parse(line.slice(5).trim()); } catch (_) { continue; }
          const text = launchLogLine(payload);
          if (!text) continue;
          _state.log += `${text}\n`;
        }
        render();
      }
    }
  } catch (error) {
    if (error && error.name !== 'AbortError') inlineError(`Launch failed: ${error.message || error}`);
  } finally {
    _stream = null;
  }
  await loadRunners(true);
}

function wire() {
  if (_wired) return;
  const modal = $(MODAL_ID);
  if (!modal) return;
  _wired = true;

  modal.addEventListener('click', (event) => {
    const target = event.target;
    if (target.closest('#close-agent-runners-modal')) { closeRunnersPanel(); return; }
    if (target.closest('[data-run-refresh]')) { loadRunners(true); return; }
    const copy = target.closest('[data-run-copy]');
    if (copy) { copyCommand(copy.dataset.runCopy); return; }
    const go = target.closest('[data-run-launch]');
    if (go) launch(go.dataset.runLaunch);
  });

  modal.addEventListener('input', (event) => {
    if (!event.target.matches('[data-run-query]')) return;
    _state.query = event.target.value;
    const host = $(BODY_ID);
    const body = host && host.querySelector('.run-table tbody');
    if (!body) return;
    const visible = sortRunners(filterRunners(normalizeCatalogue(_payload), _state.query));
    body.innerHTML = visible.map(runnerRowHtml).join('')
      || '<tr><td class="run-empty" colspan="5">No agent matches that search.</td></tr>';
  });

  $('tool-agent-runners-btn')?.addEventListener('click', () => openRunnersPanel());
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const open = $(MODAL_ID);
    if (open && !open.classList.contains('hidden')) closeRunnersPanel();
  });
}

export async function openRunnersPanel() {
  const modal = $(MODAL_ID);
  if (!modal) return;
  wire();
  _returnFocus = document.activeElement;
  modal.classList.remove('hidden');
  await loadRunners(true);
}

export function closeRunnersPanel() {
  $(MODAL_ID)?.classList.add('hidden');
  if (_stream) { try { _stream.abort(); } catch (_) {} _stream = null; }
  try { _returnFocus?.focus?.(); } catch (_) {}
}

export function initAgentRunners() {
  wire();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initAgentRunners);
} else {
  initAgentRunners();
}

const agentRunnersModule = {
  initAgentRunners,
  openRunnersPanel,
  closeRunnersPanel,
  loadRunners,
  listRunners,
  readRunner,
  runnersPageHtml,
  workerStatus,
  licenceWord,
};

if (typeof window !== 'undefined') window.agentRunnersModule = agentRunnersModule;

export default agentRunnersModule;
