// static/js/agentHarnessUI.js
// UI for the reliability harness (src/agent_harness.py) and the agent's
// Progress panel (todowrite):
//   - harness_check / harness_summary SSE events → cards inside the agent
//     thread, so the user sees when the runtime rejected an unsupported
//     "done", auto-continued a truncated answer, or verified real changes.
//   - progress_update SSE events → a docked "Progress" panel (like the task
//     list in Cowork) whose ticks carry a "verified" mark only when a tool
//     actually succeeded between updates.
// Zero dependencies on chat.js internals: it only appends to #chat-history and
// listens to the custom events below.

let API_BASE = '';
let _progressEl = null;
let _progressCollapsed = false;
let _currentSessionId = null;
let _lastTodosBySession = new Map();
let _filesBySession = new Map();     // sessionId → Map(path → workspace) of files edited in that chat

const PROGRESS_KEY = 'odysseus-progress-collapsed';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ── thread cards ────────────────────────────────────────────────────────────

function _threadForCard() {
  const chatBox = document.getElementById('chat-history');
  if (!chatBox) return null;
  // Reuse the trailing agent thread when the last visible element is one
  // (or an empty round bubble sits between us and it); otherwise open a new
  // thread so the card keeps the timeline look.
  for (let ci = chatBox.children.length - 1; ci >= Math.max(0, chatBox.children.length - 4); ci--) {
    const child = chatBox.children[ci];
    if (child.classList.contains('agent-thread')) return child;
    if (child.style.display === 'none' || child.classList.contains('agent-thinking-dots')) continue;
    if (child.classList.contains('msg') && !child.textContent.trim()) continue;
    break;
  }
  const wrap = document.createElement('div');
  wrap.className = 'agent-thread has-top';
  chatBox.appendChild(wrap);
  return wrap;
}

function _card(kind, title, bodyHtml, { open = false } = {}) {
  const thread = _threadForCard();
  if (!thread) return null;
  const node = document.createElement('div');
  node.className = `agent-thread-node harness-node harness-${kind}${open ? ' expanded' : ''}`;
  node.innerHTML =
    `<div class="agent-thread-dot"></div>` +
    `<div class="agent-thread-header harness-header"><span class="agent-thread-icon">🛡</span>` +
    `<span class="agent-thread-tool">${esc(title)}</span></div>` +
    (bodyHtml ? `<div class="agent-thread-content harness-body">${bodyHtml}</div>` : '');
  const hdr = node.querySelector('.harness-header');
  if (hdr && bodyHtml) {
    hdr.style.cursor = 'pointer';
    hdr.addEventListener('click', () => node.classList.toggle('expanded'));
  }
  thread.appendChild(node);
  try { node.scrollIntoView({ block: 'nearest' }); } catch (_) {}
  return node;
}

// Clickable file chip → fileViewer.js (data-open-file is handled globally).
function _fileChip(path, workspace, mode) {
  const p = String(path || '');
  const base = p.split(/[\\/]/).pop();
  return `<a href="#" class="harness-file" data-open-file="${esc(p)}"${workspace ? ` data-open-workspace="${esc(workspace)}"` : ''}${mode ? ` data-open-mode="${mode}"` : ''} title="${esc(p)} — click to review">${esc(base)}</a>`;
}

// "Revert all" for a turn: one button per Turn summary, confirmed, file by file
// through POST /api/workspace/revert (the same endpoint the viewer's ↺ uses).
function _revertAllButton(files, workspace) {
  const payload = esc(JSON.stringify({ files: files.slice(0, 60), workspace: workspace || null }));
  return `<button type="button" class="harness-btn harness-btn-danger" data-revert-all="${payload}" title="Undo the changes of this turn">↺ Revert all ${files.length}</button>`;
}

function _workspaceFallback() {
  try {
    if (window.workspaceModule && typeof window.workspaceModule.getWorkspace === 'function') {
      const w = window.workspaceModule.getWorkspace();
      if (w) return typeof w === 'string' ? w : (w.path || '');
    }
    const raw = localStorage.getItem('odysseus-workspace');
    if (!raw) return '';
    try { const v = JSON.parse(raw); return typeof v === 'string' ? v : (v && v.path) || ''; } catch (_) { return raw; }
  } catch (_) { return ''; }
}

async function _revertAll(button) {
  let payload;
  try { payload = JSON.parse(button.dataset.revertAll || '{}'); } catch (_) { return; }
  const files = Array.isArray(payload.files) ? payload.files : [];
  if (!files.length) return;
  const ws = payload.workspace || _workspaceFallback();
  const q = `Revert the ${files.length} file${files.length === 1 ? '' : 's'} changed in this turn? (git checkout — a new untracked file is deleted)`;
  let ok = false;
  try {
    ok = window.uiModule && window.uiModule.styledConfirm
      ? await window.uiModule.styledConfirm(q, { confirmText: 'Revert all', danger: true })
      : window.confirm(q);
  } catch (_) { ok = window.confirm(q); }
  if (!ok) return;
  button.disabled = true;
  button.textContent = 'Reverting…';
  const results = [];
  for (const f of files) {
    const name = String(f).split(/[\\/]/).pop();
    try {
      const qs = `workspace=${encodeURIComponent(ws || '')}&path=${encodeURIComponent(f)}`;
      const r = await fetch(`${API_BASE}/api/workspace/revert?${qs}`, { method: 'POST', credentials: 'same-origin' });
      let action = 'failed';
      if (r.ok) { try { action = (await r.json()).action || 'ok'; } catch (_) { action = 'ok'; } }
      else { try { action = `failed (${(await r.json()).detail || r.status})`; } catch (_) { action = `failed (${r.status})`; } }
      results.push({ name, action });
    } catch (e) { results.push({ name, action: 'failed' }); }
  }
  const okN = results.filter(r => !/^failed/.test(r.action)).length;
  button.textContent = `↺ Reverted ${okN}/${files.length}`;
  const note = document.createElement('div');
  note.className = 'harness-muted harness-revert-result';
  note.textContent = results.map(r => `${r.name}: ${r.action.replace('_', ' ')}`).join(' · ');
  button.insertAdjacentElement('afterend', note);
  try { if (window.fileViewer && window.fileViewer.isOpen()) window.fileViewer.close(); } catch (_) {}
}

const REASON_TEXT = {
  claims_without_mutation: 'The model described changes as done, but no write tool (edit_file / write_file / apply_patch) succeeded this turn.',
  fabricated_paths: 'It mentioned files that do not exist in the workspace and were never returned by any tool.',
  intent_without_action: 'It announced an action ("I will now…", "Voy a…") and ended the turn without calling any tool.',
};

export function renderHarnessCheck(json) {
  const status = json.status;
  if (status === 'auto_continue') {
    if (json.reason === 'rounds') {
      _card('continue', `Step limit (${json.round}) reached mid-task — continuing automatically with one more cycle`, '');
    } else {
      _card('continue', `Output cut off by max_tokens — continuing automatically (${json.attempt}/${json.max_attempts})`, '');
    }
    return;
  }
  if (status === 'unknown_tool') {
    const names = (json.tools || []).map(t => `<code>${esc(t)}</code>`).join(', ');
    const sugg = (json.suggestions || []).map(t => `<code>${esc(t)}</code>`).join(', ');
    _card('rejected', `The model called a tool that does not exist (${(json.tools || []).join(', ')}) — nothing ran; told it the real tool names (${json.attempt}/${json.max_attempts})`,
      `<div class="harness-foot">Called: ${names}${sugg ? ` · did you mean ${sugg}?` : ''}</div>`);
    return;
  }
  if (status === 'empty_round') {
    const open = (json.open || []).map(o => `<li>${esc(String(o))}</li>`).join('');
    _card('rejected', 'The model ended with no text and no tool call — asked it to continue or to state what remains',
      open ? `<div class="harness-foot">Open objectives:</div><ul class="harness-list">${open}</ul>` : '');
    return;
  }
  if (status === 'think_cutoff') {
    const mins = Math.round((json.seconds || 0) / 60);
    _card('continue',
      `Thinking ran for ${mins ? mins + ' min' : (json.seconds || 0) + ' s'} without any output — cut off, retrying this step with thinking OFF for the rest of the turn`,
      `<div class="harness-foot">${(json.reasoning_chars || 0).toLocaleString()} reasoning characters discarded · budget ${Math.round(json.budget_seconds || 0)} s (<code>agent_local_think_budget_seconds</code>). Pin <code>/think on</code> to disable this watchdog.</div>`);
    return;
  }
  const reasons = Array.isArray(json.reasons) ? json.reasons : [];
  const items = reasons.map(r => `<li>${esc(REASON_TEXT[r] || r)}</li>`);
  if (json.bad_paths && json.bad_paths.length) {
    items.push(`<li>Non-existent paths: ${json.bad_paths.map(p => `<code>${esc(p)}</code>`).join(', ')}</li>`);
  }
  if (json.intent) {
    items.push(`<li>Announced: <em>${esc(String(json.intent).slice(0, 140))}</em></li>`);
  }
  if (json.claims && json.claims.length) {
    items.push(`<li>Claims: ${json.claims.slice(0, 2).map(c => `<em>${esc(String(c).slice(0, 120))}</em>`).join(' · ')}</li>`);
  }
  if (status === 'syntax_error') {
    const errs = (json.errors || []).map(e => `<li><code>${esc(e.path)}</code>: ${esc(e.error)}</li>`).join('');
    _card('rejected', 'Syntax check failed in changed files — asked the model to fix it', `<ul class="harness-list">${errs}</ul>`, { open: true });
    return;
  }
  if (status === 'rejected') {
    _card('rejected',
      `Harness check failed (attempt ${json.attempt}/${json.max_attempts}) — asked the model to do the work for real`,
      `<ul class="harness-list">${items.join('')}</ul>`, { open: true });
  } else if (status === 'unverified') {
    _card('unverified',
      'Unverified answer — the text below is NOT backed by tool evidence',
      `<ul class="harness-list">${items.join('')}</ul><div class="harness-foot">Files actually modified this turn: ${json.mutations && json.mutations.length ? json.mutations.map(esc).join(', ') : '<b>none</b>'}.</div>`,
      { open: true });
  } else if (status === 'verified') {
    const files = (json.mutations || []);
    const checks = Array.isArray(json.static_checks) ? json.static_checks : [];
    const checked = checks.filter(c => c.ok).length;
    const body = [];
    if (files.length) body.push(`<div class="harness-foot harness-files">${files.map(f => _fileChip(f, json.workspace, 'diff')).join(' ')}</div>`);
    if (checked) body.push(`<div class="harness-foot">Syntax check passed: ${checks.filter(c => c.ok).map(c => `<code>${esc(c.path)}</code>`).join(' ')}</div>`);
    _card('verified',
      files.length ? `Verified: ${files.length} file${files.length === 1 ? '' : 's'} changed${checked ? ` · ${checked} syntax-checked` : ''}` : 'Verified against the tool log',
      body.join(''));
  }
}

export function renderHarnessSummary(json) {
  const d = json.data || {};
  const tools = d.tool_calls || 0;
  const failed = d.failed_calls || 0;
  const files = d.mutations || [];
  const git = d.git;
  const stop = d.stop_reason || 'complete';
  const parts = [];
  parts.push(`${tools} tool call${tools === 1 ? '' : 's'}${failed ? ` (${failed} failed)` : ''}`);
  parts.push(files.length ? `Edited ${files.length} file${files.length === 1 ? '' : 's'}` : 'no files changed');
  if (git && typeof git.changed_count === 'number') {
    parts.push(git.changed_count ? `git: ${git.changed_count} path${git.changed_count === 1 ? '' : 's'} dirty${git.shortstat ? ` (${git.shortstat.trim()})` : ''}` : 'git: clean');
  }
  if (d.rejections) parts.push(`${d.rejections} rejection${d.rejections === 1 ? '' : 's'}`);
  if (d.length_continues) parts.push(`${d.length_continues} auto-continue`);
  const stopLabel = {
    complete: 'finished', complete_unverified: 'finished — UNVERIFIED', rounds_exhausted: 'step limit',
    budget_exceeded: 'tool budget', loop_breaker: 'loop breaker', intent_nudge_exhausted: 'stalled',
    awaiting_user: 'waiting for you', length: 'cut off',
  }[stop] || stop;
  const details = [];
  if (files.length) details.push(`<div class="harness-files"><b>Edited:</b> ${files.map(f => _fileChip(f, d.workspace, 'diff')).join(' ')} <span class="harness-muted">— click a file to review it (contents / diff / open folder)</span></div>`);
  if (files.length) details.push(`<div class="harness-actions">${_revertAllButton(files, d.workspace)} <span class="harness-muted">undo this turn's edits (git checkout per file; a new untracked file is deleted)</span></div>`);
  if (git && git.changed && git.changed.length) {
    details.push(`<div><b>git status:</b><pre class="harness-pre">${esc(git.changed.map(c => `${c.status.padEnd(2)} ${c.path}`).join('\n'))}</pre></div>`);
  }
  if (d.tools_run) {
    details.push(`<div><b>Tools:</b> ${Object.entries(d.tools_run).map(([k, v]) => `${esc(k)}×${v}`).join(', ')}</div>`);
  }
  if (Array.isArray(d.finish_reasons) && d.finish_reasons.length) {
    details.push(`<div><b>finish_reason per round:</b> ${d.finish_reasons.map(r => esc(r || '?')).join(' → ')}</div>`);
  }
  // Runtime notes worth a human glance (whole-file rewrites, cut-offs, unknown paths).
  const notes = Array.isArray(d.notes) ? d.notes : [];
  const NOTE_TEXT = {
    whole_file_rewrite: p => `<b>⚠ whole-file rewrite</b> of <code>${esc(p)}</code> (write_file dropped ≥5 lines) — review the diff`,
    think_cutoff: r => `thinking cut off in round ${esc(r)}, rest of the turn ran with thinking off`,
    unverified_mentions: p => `mentions paths never seen in a tool result: <code>${esc(p)}</code>`,
    auto_continue_rounds: r => `step limit reached at round ${esc(r)}, one extra cycle granted`,
    empty_round_nudge: r => `round ${esc(r)} was empty (no text, no tool) — nudged`,
  };
  const noteLines = notes.map(n => {
    const [k, ...rest] = String(n).split(/[:@]/);
    const fn = NOTE_TEXT[k];
    return fn ? fn(rest.join(':')) : esc(String(n));
  });
  if (noteLines.length) details.push(`<div><b>Notes:</b><ul class="harness-list">${noteLines.map(l => `<li>${l}</li>`).join('')}</ul></div>`);
  const rewrote = notes.some(n => String(n).startsWith('whole_file_rewrite:'));
  if (rewrote) parts.push('⚠ whole-file rewrite');
  const kind = stop === 'complete_unverified' ? 'unverified' : (files.length ? 'verified' : 'summary');
  _card(kind, `Turn summary · ${parts.join(' · ')} · ${stopLabel}`, details.join(''), { open: files.length > 0 });
}

// ── Sub-agent board (delegate_agents) ────────────────────────────────────────

let _boards = new Map(); // toolNode/thread key → board element

function _boardFor() {
  const chatBox = document.getElementById('chat-history');
  if (!chatBox) return null;
  // The delegate_agents tool card is the last running node in the thread;
  // attach the board right after it so workers appear where the call is.
  let anchor = null;
  const threads = chatBox.querySelectorAll('.agent-thread');
  const thread = threads.length ? threads[threads.length - 1] : _threadForCard();
  if (!thread) return null;
  let board = thread.querySelector('.subagent-board:last-of-type');
  if (board && board.dataset.open === '1') return board;
  board = document.createElement('div');
  board.className = 'agent-thread-node harness-node harness-subagents expanded subagent-board';
  board.dataset.open = '1';
  board.innerHTML = `<div class="agent-thread-dot"></div><div class="agent-thread-header harness-header"><span class="agent-thread-icon">🤖</span><span class="agent-thread-tool">Sub-agents</span><span class="subagent-board-count"></span></div><div class="agent-thread-content harness-body"><div class="subagent-rows"></div></div>`;
  thread.appendChild(board);
  return board;
}

const SA_STATUS_ICON = { started: '◉', running: '◉', done: '✓', error: '✗' };

export function renderSubagentEvent(json) {
  const sa = json.subagent || {};
  const board = _boardFor();
  if (!board) return;
  const rows = board.querySelector('.subagent-rows');
  let row = rows.querySelector(`[data-sa="${sa.id}"]`);
  if (!row) {
    row = document.createElement('div');
    row.className = 'subagent-row is-running';
    row.dataset.sa = sa.id;
    row.innerHTML = `<div class="subagent-head"><span class="subagent-icon">◉</span><span class="subagent-name"></span><span class="subagent-meta"></span></div><div class="subagent-last"></div>`;
    rows.appendChild(row);
  }
  row.querySelector('.subagent-name').textContent = `${(sa.index ?? 0) + 1}. ${sa.name || 'worker'}`;
  const meta = row.querySelector('.subagent-meta');
  const last = row.querySelector('.subagent-last');
  const ev = sa.event;
  if (ev === 'started') {
    last.textContent = sa.instruction || '';
    if (sa.session_id) meta.innerHTML = `<a href="#${esc(sa.session_id)}" class="subagent-chat-link" title="Open this worker's chat">chat ${esc(sa.session_id)}</a>`;
  } else if (ev === 'tool') {
    const n = (parseInt(row.dataset.tools || '0', 10) + (sa.phase === 'done' ? 1 : 0));
    row.dataset.tools = String(n);
    last.textContent = `${sa.phase === 'start' ? '▶' : (sa.ok === false ? '✗' : '✓')} ${sa.tool || ''} ${sa.command || sa.output || ''}`.trim();
    const link = meta.querySelector('a');
    meta.innerHTML = `${n} tool${n === 1 ? '' : 's'}` + (link ? ` · ${link.outerHTML}` : '');
  } else if (ev === 'harness') {
    last.textContent = `🛡 ${sa.status}${sa.reasons && sa.reasons.length ? ': ' + sa.reasons.join(', ') : ''}`;
  } else if (ev === 'guard') {
    last.textContent = `⚠ ${sa.kind}`;
  } else if (ev === 'error') {
    row.className = 'subagent-row is-error';
    row.querySelector('.subagent-icon').textContent = '✗';
    last.textContent = sa.message || 'error';
  } else if (ev === 'done') {
    const ok = !sa.error && (sa.stop_reason === 'complete');
    row.className = `subagent-row ${sa.error ? 'is-error' : (ok ? 'is-done' : 'is-partial')}`;
    row.querySelector('.subagent-icon').textContent = sa.error ? '✗' : (ok ? '✓' : '◑');
    const files = (sa.mutations || []).length;
    const link = meta.querySelector('a');
    meta.innerHTML = `${sa.tool_calls || 0} tools${sa.failed_calls ? ` (${sa.failed_calls} failed)` : ''} · ${files ? `${files} file${files === 1 ? '' : 's'} changed` : 'no files changed'} · ${sa.duration_s || 0}s · ${esc(sa.stop_reason || '')}` + (link ? ` · ${link.outerHTML}` : '');
    last.textContent = sa.final_text || '';
  }
  const all = rows.querySelectorAll('.subagent-row');
  const done = rows.querySelectorAll('.subagent-row.is-done, .subagent-row.is-error, .subagent-row.is-partial').length;
  board.querySelector('.subagent-board-count').textContent = ` ${done}/${all.length}`;
  if (done === all.length && all.length) board.dataset.open = '0';
  try { row.scrollIntoView({ block: 'nearest' }); } catch (_) {}
}

// ── Progress panel ──────────────────────────────────────────────────────────

function _ensureProgressEl() {
  if (_progressEl && document.body.contains(_progressEl)) return _progressEl;
  const host = document.getElementById('chat-container') || document.body;
  const el = document.createElement('aside');
  el.id = 'agent-progress-panel';
  el.className = 'agent-progress-panel';
  el.hidden = true;
  el.innerHTML =
    `<div class="agent-progress-head">` +
    `<button type="button" class="agent-progress-toggle" title="Collapse / expand" aria-label="Collapse or expand progress"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></button>` +
    `<span class="agent-progress-title">Progress</span><span class="agent-progress-count"></span>` +
    `<button type="button" class="agent-progress-close" title="Hide" aria-label="Hide progress">×</button>` +
    `</div><ol class="agent-progress-list"></ol>` +
    `<div class="agent-progress-files" hidden><div class="agent-progress-files-head">Files edited in this chat <span class="agent-progress-files-count"></span></div><div class="agent-progress-files-list harness-files"></div></div>`;
  host.appendChild(el);
  el.querySelector('.agent-progress-toggle').addEventListener('click', () => {
    _progressCollapsed = !_progressCollapsed;
    el.classList.toggle('collapsed', _progressCollapsed);
    try { localStorage.setItem(PROGRESS_KEY, _progressCollapsed ? '1' : '0'); } catch (_) {}
  });
  el.querySelector('.agent-progress-close').addEventListener('click', () => { el.hidden = true; });
  try { _progressCollapsed = localStorage.getItem(PROGRESS_KEY) === '1'; } catch (_) {}
  el.classList.toggle('collapsed', _progressCollapsed);
  _progressEl = el;
  return el;
}

const STATUS_ICON = { pending: '○', in_progress: '◉', completed: '✓' };
// Objectives that imply a file change (EN/ES) — used for the "no write" tag.
const CHANGE_TODO_RE = /\b(?:add|create|implement|fix|update|remove|delete|refactor|rename|write|edit|modify|change|wire|hook|patch|install|configure|a[ñn]adir|a[ñn]ade|agregar|crear|crea|implementar|implementa|arreglar|arregla|corregir|corrige|actualizar|actualiza|eliminar|elimina|borrar|borra|modificar|modifica|cambiar|cambia|escribir|escribe|editar|edita|refactorizar|renombrar|configurar|instalar)\b/i;

export function renderProgress(todos, { sessionId = null } = {}) {
  const el = _ensureProgressEl();
  const list = el.querySelector('.agent-progress-list');
  const count = el.querySelector('.agent-progress-count');
  if (sessionId) _lastTodosBySession.set(sessionId, todos);
  if (!Array.isArray(todos) || !todos.length) {
    list.innerHTML = '';
    count.textContent = '';
    _renderFiles(sessionId || _currentSessionId);
    return;
  }
  const done = todos.filter(t => t.status === 'completed').length;
  count.textContent = `${done}/${todos.length}`;
  list.innerHTML = todos.map((t, i) => {
    const st = t.status || 'pending';
    const unverified = st === 'completed' && t.verified === false;
    // A change-type objective ticked off without any successful write since
    // the previous update: the tick may be premature.
    const noWrite = st === 'completed' && !unverified && t.mutation_backed === false && CHANGE_TODO_RE.test(t.content || '');
    const cls = `agent-progress-item is-${st}${unverified ? ' is-unverified' : ''}${noWrite ? ' is-nowrite' : ''}`;
    let tag = '';
    if (unverified) tag = `<span class="agent-progress-tag" title="Marked done, but no tool succeeded since the previous update">unverified</span>`;
    else if (noWrite) tag = `<span class="agent-progress-tag is-nowrite" title="Marked done, but no file was changed since the previous update">no write</span>`;
    return `<li class="${cls}"><span class="agent-progress-num">${i + 1}</span><span class="agent-progress-icon">${STATUS_ICON[st] || '○'}</span><span class="agent-progress-text">${esc(t.content)}</span>${tag}</li>`;
  }).join('');
  el.hidden = false;
  _renderFiles(sessionId || _currentSessionId);
}

/** Files the agent edited in this chat, across turns (chips → file viewer). */
export function noteMutations(sessionId, files, workspace) {
  if (!sessionId || !Array.isArray(files) || !files.length) return;
  let m = _filesBySession.get(sessionId);
  if (!m) { m = new Map(); _filesBySession.set(sessionId, m); }
  for (const f of files) if (f) m.set(String(f), workspace || m.get(String(f)) || null);
  if (sessionId === _currentSessionId) _renderFiles(sessionId);
}

function _renderFiles(sessionId) {
  const el = _ensureProgressEl();
  const box = el.querySelector('.agent-progress-files');
  if (!box) return;
  const m = sessionId ? _filesBySession.get(sessionId) : null;
  const hasTodos = el.querySelector('.agent-progress-list').children.length > 0;
  if (!m || !m.size) {
    box.hidden = true;
    if (!hasTodos) el.hidden = true;
    return;
  }
  box.hidden = false;
  box.querySelector('.agent-progress-files-count').textContent = `(${m.size})`;
  box.querySelector('.agent-progress-files-list').innerHTML = [...m.entries()].map(([f, ws]) => _fileChip(f, ws, 'diff')).join(' ');
  el.hidden = false;
}

export function clearProgress() {
  if (_progressEl) {
    _progressEl.hidden = true;
    const list = _progressEl.querySelector('.agent-progress-list');
    if (list) list.innerHTML = '';
    const box = _progressEl.querySelector('.agent-progress-files');
    if (box) box.hidden = true;
  }
}

export async function restoreProgress(sessionId) {
  _currentSessionId = sessionId;
  if (!sessionId) { clearProgress(); return; }
  const cached = _lastTodosBySession.get(sessionId);
  if (cached) { renderProgress(cached, { sessionId }); return; }
  clearProgress();
  _renderFiles(sessionId);
  try {
    const r = await fetch(`${API_BASE}/api/agent/progress/${encodeURIComponent(sessionId)}`, { credentials: 'same-origin' });
    if (!r.ok) return;
    const data = await r.json();
    if (_currentSessionId !== sessionId) return; // switched again meanwhile
    if (data && Array.isArray(data.todos) && data.todos.length) {
      renderProgress(data.todos, { sessionId });
    }
  } catch (_) { /* offline / no progress yet */ }
}

// ── event wiring ─────────────────────────────────────────────────────────────

export function handleStreamEvent(json, { sessionId = null } = {}) {
  switch (json.type) {
    case 'harness_check': renderHarnessCheck(json); return true;
    case 'harness_summary':
      try { const d = json.data || {}; noteMutations(sessionId || _currentSessionId, d.mutations || [], d.workspace || null); } catch (_) {}
      renderHarnessSummary(json); return true;
    case 'progress_update': renderProgress(json.todos || [], { sessionId }); return true;
    case 'tool_progress':
      if (json.subagent) { renderSubagentEvent(json); return true; }
      return false;
    default: return false;
  }
}

export function init(apiBase) {
  API_BASE = apiBase || '';
  document.addEventListener('odysseus:session-switch', (ev) => {
    const id = ev.detail && ev.detail.id;
    restoreProgress(id || null);
  });
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-revert-all]');
    if (!b || b.disabled) return;
    e.preventDefault();
    e.stopPropagation();
    _revertAll(b);
  });
}

const agentHarnessUI = { init, handleStreamEvent, renderHarnessCheck, renderHarnessSummary, renderProgress, restoreProgress, clearProgress, renderSubagentEvent, noteMutations };
window.agentHarnessUI = agentHarnessUI;
export default agentHarnessUI;
