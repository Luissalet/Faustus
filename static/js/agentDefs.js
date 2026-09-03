// static/js/agentDefs.js
// Agent definitions — what each agent on this machine may and may not do.
//
// The backend is authoritative (src/agent_defs.py, routes/agent_def_routes.py):
// a definition is an AGENT.md, frontmatter plus a body, and the API resolves it
// into rules before it gets here. This module is the human end: one card per
// agent, the rules as sentences, and a button that puts its slug on a dispatch.
//
// Four honesty rules run through this file and are not negotiable:
//
//   * the page shows RESOLVED RULES, never the raw frontmatter. A reader who
//     has to compile an allowlist and an ordered rule list in their head to
//     know whether an agent can write to src/ will get it wrong, and the whole
//     point of putting an agent in a file is that they do not have to;
//   * a file that would NOT load is shown with its reason, next to the ones
//     that did. A definition that vanishes from a list without a word is how
//     someone comes to believe a restriction is in force that is not;
//   * the sentence the feature turns on is printed above the list, not in a
//     tooltip: a path rule does not reach inside `bash` or `python`. A
//     definition that keeps a shell and denies a path says so on its own card;
//   * "asks to delegate" and "may delegate" are two facts and stay two — the
//     depth ceiling is what separates them, and it is named.
//
// The renderers are pure and live between the marked region below, so
// tests/test_agent_defs.py can run them in bare node.

const API = `${window.location.origin}/api/agent-defs`;

// ── Agent definitions: pure helpers (dependency-free; extracted and run under node by tests) ──
// Everything between these markers must stay free of DOM, module and window
// references so the page test can execute it in bare node.

/** Local escape: same table as ui.js esc(), but import-free for tests. */
function defEsc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

/** Sanitize a server word (mode, source, effect) for use in a class name. */
function defToken(value) {
  return String(value == null ? '' : value).toLowerCase().replace(/[^a-z0-9_-]/g, '');
}

/** The three modes the backend may send. Anything else reads as `worker`. */
const DEF_MODES = ['coordinator', 'worker', 'reviewer'];
/** Where a definition came from. Anything else reads as `user`. */
const DEF_SOURCES = ['builtin', 'user', 'repo'];

/** What each mode means, for the title attribute. Never a claim beyond it. */
function modeHint(mode) {
  if (mode === 'coordinator') return 'May split its work between further workers, up to the depth ceiling.';
  if (mode === 'reviewer') return 'The only mode allowed to fill the reviewer slot, which runs after everyone with the file locks off.';
  return 'Does one task. Cannot start another worker.';
}

/** Where the file lives, in words. */
function sourceHint(source) {
  if (source === 'builtin') return 'Shipped with Faustus. Put a file with the same slug under DATA_DIR/agents to replace it.';
  if (source === 'repo') return 'Carried by this folder. It loaded because you approved this folder’s instruction files.';
  return 'Yours, under DATA_DIR/agents.';
}

/** One definition row (routes/agent_def_routes._row), with every field defaulted. */
function normalizeDef(raw) {
  const row = raw && typeof raw === 'object' ? raw : {};
  const mode = DEF_MODES.includes(String(row.mode)) ? String(row.mode) : 'worker';
  const source = DEF_SOURCES.includes(String(row.source)) ? String(row.source) : 'user';
  const rules = Array.isArray(row.rules) ? row.rules.filter(r => r && typeof r === 'object') : [];
  return {
    slug: String(row.slug == null ? '' : row.slug),
    name: String(row.name || row.slug || ''),
    description: String(row.description || ''),
    mode,
    source,
    model: String(row.model || ''),
    endpoint_id: String(row.endpoint_id || ''),
    runner: String(row.runner || ''),
    path: String(row.path || ''),
    may_delegate: Boolean(row.may_delegate),
    caveats: Array.isArray(row.caveats) ? row.caveats.map(String) : [],
    rules: rules.map(r => ({
      effect: String(r.effect) === 'deny' ? 'deny' : 'allow',
      what: String(r.what || ''),
      detail: String(r.detail || ''),
    })),
  };
}

/** GET /api/agent-defs: {"agents":[…],"errors":[…]}, or a bare list. */
function normalizeDefs(raw) {
  let list = [];
  if (Array.isArray(raw)) list = raw;
  else if (raw && typeof raw === 'object' && Array.isArray(raw.agents)) list = raw.agents;
  return list
    .filter(row => row && typeof row === 'object' && row.slug != null && String(row.slug))
    .map(normalizeDef);
}

/** The files that would not load, each with its reason. Never dropped. */
function normalizeErrors(raw) {
  const list = raw && typeof raw === 'object' && Array.isArray(raw.errors) ? raw.errors : [];
  return list
    .filter(row => row && typeof row === 'object')
    .map(row => ({
      path: String(row.path || ''),
      slug: String(row.slug || ''),
      reason: String(row.reason || 'no reason given'),
    }));
}

/** Yours and the repo's first, then the built-ins, then by name. */
function sortDefs(rows) {
  const order = { repo: 0, user: 1, builtin: 2 };
  return (rows || []).map(normalizeDef).slice().sort((a, b) => {
    if (order[a.source] !== order[b.source]) return order[a.source] - order[b.source];
    return a.name.localeCompare(b.name);
  });
}

function filterDefs(rows, query) {
  const all = (rows || []).map(normalizeDef);
  const needle = String(query == null ? '' : query).trim().toLowerCase();
  if (!needle) return all;
  return all.filter(row => (
    row.slug.toLowerCase().includes(needle)
    || row.name.toLowerCase().includes(needle)
    || row.description.toLowerCase().includes(needle)
    || row.rules.some(rule => rule.detail.toLowerCase().includes(needle))
  ));
}

/**
 * Whether this agent may delegate, in words — the two facts kept apart. A
 * coordinator that cannot delegate because of the depth ceiling says so and
 * names the ceiling, rather than reading as a coordinator that can.
 */
function delegateStatus(row, maxDepth) {
  const def = normalizeDef(row);
  const ceiling = Number.isFinite(Number(maxDepth)) ? Number(maxDepth) : 1;
  if (!def.may_delegate) {
    return { can: false, label: 'cannot delegate', detail: 'It does one task and reports back.' };
  }
  if (ceiling < 1) {
    return { can: false, label: 'cannot delegate',
             detail: 'Asks to, and the depth ceiling is 0: no worker may start another.' };
  }
  return { can: true, label: 'may delegate',
           detail: `Its own workers are the last generation (depth ceiling ${ceiling}).` };
}

/** One rule, as a line. */
function ruleHtml(rule) {
  const effect = String(rule.effect) === 'deny' ? 'deny' : 'allow';
  return `<li class="def-rule is-${effect}"><span class="def-rule-effect">${defEsc(effect)}</span>`
    + `<span class="def-rule-what">${defEsc(rule.what)}</span>`
    + `<span class="def-rule-detail">${defEsc(rule.detail)}</span></li>`;
}

/** One card. */
function defCardHtml(raw, opts = {}) {
  const row = normalizeDef(raw);
  const del = delegateStatus(row, opts.maxDepth);
  const where = [row.model && `model ${row.model}`, row.endpoint_id && `endpoint ${row.endpoint_id}`,
                 row.runner && `runner ${row.runner}`].filter(Boolean).join(' · ');
  return `
    <article class="def-card" data-def-slug="${defEsc(row.slug)}">
      <header class="def-card-head">
        <span class="def-name">${defEsc(row.name)}</span>
        <code class="def-slug">${defEsc(row.slug)}</code>
        <span class="def-mode is-${defToken(row.mode)}" title="${defEsc(modeHint(row.mode))}">${defEsc(row.mode)}</span>
        <span class="def-source is-${defToken(row.source)}" title="${defEsc(sourceHint(row.source))}">${defEsc(row.source)}</span>
      </header>
      ${row.description ? `<p class="def-desc">${defEsc(row.description)}</p>` : ''}
      ${where ? `<p class="def-route">${defEsc(where)}</p>` : ''}
      <ul class="def-rules">${row.rules.map(ruleHtml).join('')}</ul>
      <p class="def-delegate${del.can ? ' is-yes' : ''}">${defEsc(del.label)} — ${defEsc(del.detail)}</p>
      ${row.caveats.map(c => `<p class="def-caveat">${defEsc(c)}</p>`).join('')}
      <div class="def-actions">
        <button type="button" class="def-btn def-pick" data-def-pick="${defEsc(row.slug)}"
                aria-label="Use ${defEsc(row.name)} for a dispatched task">Use for a task</button>
        <code class="def-usage">"agent": "${defEsc(row.slug)}"</code>
        <button type="button" class="def-btn def-copy" data-def-copy="${defEsc(row.slug)}"
                aria-label="Copy the slug of ${defEsc(row.name)}">Copy</button>
      </div>
    </article>`;
}

/** The whole page body. */
function defsPageHtml(payload, opts = {}) {
  const state = opts || {};
  const data = payload && typeof payload === 'object' ? payload : {};
  const all = normalizeDefs(data);
  const visible = sortDefs(filterDefs(all, state.query));
  const errors = normalizeErrors(data);
  const maxDepth = Number.isFinite(Number(data.max_depth)) ? Number(data.max_depth) : 1;
  const setting = String(data.depth_setting || 'agent_subagent_depth');
  const cards = state.loading
    ? '<p class="def-empty">Loading agent definitions…</p>'
    : (visible.map(row => defCardHtml(row, { maxDepth })).join('') || `<p class="def-empty">${
      all.length ? 'No agent matches that search.'
        : 'No agent definitions: not even the built-ins could be read.'}</p>`);
  // Every file that would not load, with its reason, in the list — not in a log.
  const errorBlock = errors.length ? `
    <div class="def-errors">
      <p class="def-errors-head">${errors.length} definition file(s) did not load. They are not in force:</p>
      <ul>${errors.map(e => `<li><code>${defEsc(e.path || e.slug)}</code> — ${defEsc(e.reason)}</li>`).join('')}</ul>
    </div>` : '';
  return `
    <div class="def-head">
      <div>
        <h2 class="def-title">Agent definitions</h2>
        <p class="def-desc-page">An agent is a file: what it may use, what it may touch, where it runs. Put its slug on a dispatched task and the worker starts under it.</p>
      </div>
      <div class="def-toolbar">
        <input type="search" class="def-search" data-def-query placeholder="Search agents" aria-label="Search agent definitions" value="${defEsc(state.query || '')}" spellcheck="false" />
        <button type="button" class="def-btn" data-def-refresh>Refresh</button>
      </div>
    </div>
    <p class="def-shell"><strong>What a path rule cannot promise:</strong> ${defEsc(String(data.shell_note || 'a path rule governs the file tools, not another program’s shell.'))}</p>
    <p class="def-counts">${all.length} definition${all.length === 1 ? '' : 's'} · delegation depth ceiling ${maxDepth} (<code>${defEsc(setting)}</code>)</p>
    <p class="def-error" data-def-error${state.error ? '' : ' hidden'}>${defEsc(state.error || '')}</p>
    ${errorBlock}
    <div class="def-list">${cards}</div>`;
}

// ── Agent definitions: end pure helpers ──

export { defsPageHtml, defCardHtml, normalizeDefs, normalizeErrors, sortDefs, filterDefs,
         delegateStatus, modeHint, sourceHint };

const MODAL_ID = 'agent-defs-modal';
const BODY_ID = 'agent-defs-body';

let _payload = null;
let _state = { query: '', error: '', loading: false };
let _wired = false;
let _loaded = false;
let _returnFocus = null;

const $ = (id) => (typeof document === 'undefined' ? null : document.getElementById(id));

/** fetch wrapper for /api/agent-defs/*: a non-2xx becomes an Error. */
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

/** The bound folder, so the repo's own definitions are asked for too. */
function activeWorkspace() {
  try {
    return localStorage.getItem('odysseus-workspace') || '';
  } catch (_) {
    return '';
  }
}

export const listDefs = () => {
  const ws = activeWorkspace();
  return req(ws ? `?workspace=${encodeURIComponent(ws)}` : '');
};
export const readDef = (slug) => req(`/${encodeURIComponent(slug)}`);

// The panel's own styles, injected once. They live here rather than in
// static/style.css because this page is self-contained: everything it needs to
// exist is one script tag, and nothing else in the app reads a `def-` class.
const STYLE_ID = 'agent-defs-style';
const STYLE = `
.defs-page .defs-modal-content{max-width:70rem;width:92vw;max-height:88vh;overflow:auto;padding:1.25rem}
.defs-close-btn{position:absolute;top:.6rem;right:.8rem;background:none;border:0;font-size:1.4rem;
  line-height:1;cursor:pointer;color:inherit;opacity:.6}
.def-head{display:flex;gap:1rem;justify-content:space-between;align-items:flex-start;flex-wrap:wrap}
.def-title{margin:0 0 .25rem}
.def-desc-page,.def-shell,.def-counts{margin:.25rem 0;opacity:.85;font-size:.85rem}
.def-shell{border-left:3px solid currentColor;padding-left:.6rem;opacity:.95}
.def-toolbar{display:flex;gap:.5rem;align-items:center}
.def-btn{cursor:pointer;padding:.3rem .7rem;border-radius:.4rem;border:1px solid currentColor;
  background:none;color:inherit;font:inherit;font-size:.8rem}
.def-error{color:#e06c75;font-size:.85rem}
.def-errors{border:1px solid #e06c75;border-radius:.5rem;padding:.6rem .9rem;margin:.6rem 0;font-size:.82rem}
.def-errors-head{margin:0 0 .3rem;font-weight:600}
.def-errors ul{margin:0;padding-left:1.1rem}
.def-list{display:grid;gap:.9rem;grid-template-columns:repeat(auto-fill,minmax(21rem,1fr));margin-top:.9rem}
.def-card{border:1px solid currentColor;border-radius:.6rem;padding:.8rem .9rem;opacity:.96}
.def-card-head{display:flex;gap:.45rem;align-items:baseline;flex-wrap:wrap}
.def-name{font-weight:600}
.def-slug,.def-usage{font-size:.75rem;opacity:.75}
.def-mode,.def-source{font-size:.7rem;text-transform:uppercase;letter-spacing:.04em;
  border:1px solid currentColor;border-radius:.3rem;padding:0 .3rem;opacity:.75}
.def-desc,.def-route{margin:.4rem 0;font-size:.83rem;opacity:.85}
.def-rules{list-style:none;margin:.5rem 0;padding:0;display:grid;gap:.2rem;font-size:.8rem}
.def-rule{display:flex;gap:.4rem;align-items:baseline}
.def-rule-effect{min-width:3rem;font-weight:600;text-transform:uppercase;font-size:.68rem;opacity:.8}
.def-rule.is-deny .def-rule-effect{color:#e06c75}
.def-rule.is-allow .def-rule-effect{color:#98c379}
.def-rule-what{opacity:.6;min-width:4rem;font-size:.72rem}
.def-delegate{margin:.4rem 0;font-size:.8rem;opacity:.8}
.def-delegate.is-yes{opacity:1}
.def-caveat{margin:.3rem 0;font-size:.78rem;border-left:3px solid #e5c07b;padding-left:.5rem;opacity:.9}
.def-actions{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-top:.5rem}
.def-empty{opacity:.7;font-size:.85rem}
`;

function injectStyle() {
  if (typeof document === 'undefined' || document.getElementById(STYLE_ID)) return;
  const tag = document.createElement('style');
  tag.id = STYLE_ID;
  tag.textContent = STYLE;
  document.head.appendChild(tag);
}

/**
 * The modal is built here rather than in index.html: this page is one list and
 * needs no markup nobody else uses, and a panel that carries its own DOM can
 * be added to a build by loading one script.
 */
function host() {
  let modal = $(MODAL_ID);
  if (modal) return modal;
  if (typeof document === 'undefined') return null;
  injectStyle();
  modal = document.createElement('div');
  modal.id = MODAL_ID;
  modal.className = 'modal defs-page hidden';
  modal.innerHTML = `
    <div class="modal-content defs-modal-content">
      <button type="button" class="defs-close-btn" id="close-agent-defs-modal"
              aria-label="Close agent definitions" title="Back to chat">×</button>
      <div id="${BODY_ID}" class="defs-body"></div>
    </div>`;
  document.body.appendChild(modal);
  return modal;
}

function render() {
  const body = $(BODY_ID);
  if (!body) return;
  body.innerHTML = defsPageHtml(_payload, _state);
}

function inlineError(message) {
  _state.error = message || '';
  const slot = $(BODY_ID)?.querySelector('[data-def-error]');
  if (!slot) { render(); return; }
  slot.textContent = _state.error;
  slot.hidden = !_state.error;
}

export async function loadDefs(force = false) {
  if (_loaded && !force) return _payload;
  _state.loading = true;
  _state.error = '';
  render();
  try {
    _payload = await listDefs();
    _loaded = true;
  } catch (error) {
    _payload = null;
    _state.error = `Could not read the agent definitions: ${error.message || error}`;
  } finally {
    _state.loading = false;
    render();
  }
  return _payload;
}

async function copySlug(slug) {
  try {
    await navigator.clipboard.writeText(`"agent": "${String(slug || '')}"`);
    inlineError('');
  } catch (_) {
    inlineError('Could not copy — select the slug and copy it by hand.');
  }
}

/**
 * Pick this agent for the next dispatch. The choice is announced rather than
 * applied from in here: the dispatch form is somebody else's module, and a
 * page that reaches into it would break the day that module moves.
 */
export function pickAgent(slug) {
  const chosen = String(slug || '');
  if (!chosen) return '';
  try {
    localStorage.setItem('faustus-dispatch-agent', chosen);
  } catch (_) { /* private mode: the event below still carries it */ }
  try {
    document.dispatchEvent(new CustomEvent('faustus:dispatch-agent', { detail: { agent: chosen } }));
  } catch (_) { /* no CustomEvent: the stored value is the fallback */ }
  closeDefsPanel();
  return chosen;
}

function wire() {
  if (_wired) return;
  const modal = host();
  if (!modal) return;
  _wired = true;

  modal.addEventListener('click', (event) => {
    const target = event.target;
    if (target.closest('#close-agent-defs-modal')) { closeDefsPanel(); return; }
    if (target.closest('[data-def-refresh]')) { loadDefs(true); return; }
    const copy = target.closest('[data-def-copy]');
    if (copy) { copySlug(copy.dataset.defCopy); return; }
    const pick = target.closest('[data-def-pick]');
    if (pick) pickAgent(pick.dataset.defPick);
  });

  modal.addEventListener('input', (event) => {
    if (!event.target.matches('[data-def-query]')) return;
    _state.query = event.target.value;
    render();
  });

  $('tool-agent-defs-btn')?.addEventListener('click', () => openDefsPanel());
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const open = $(MODAL_ID);
    if (open && !open.classList.contains('hidden')) closeDefsPanel();
  });
}

export async function openDefsPanel() {
  wire();
  const modal = $(MODAL_ID);
  if (!modal) return;
  _returnFocus = document.activeElement;
  modal.classList.remove('hidden');
  await loadDefs(true);
}

export function closeDefsPanel() {
  $(MODAL_ID)?.classList.add('hidden');
  try { _returnFocus?.focus?.(); } catch (_) { /* the element went away */ }
}

export function initAgentDefs() {
  wire();
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAgentDefs);
  } else {
    initAgentDefs();
  }
}

const agentDefsModule = {
  initAgentDefs,
  openDefsPanel,
  closeDefsPanel,
  loadDefs,
  listDefs,
  readDef,
  pickAgent,
  defsPageHtml,
  delegateStatus,
};

if (typeof window !== 'undefined') window.agentDefsModule = agentDefsModule;

export default agentDefsModule;
