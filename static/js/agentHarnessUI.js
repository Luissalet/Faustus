// static/js/agentHarnessUI.js
// UI for the reliability harness (src/agent_harness.py) and the agent's
// Progress panel (todowrite):
//   - harness_check / harness_summary SSE events → cards inside the agent
//     thread, so the user sees when the runtime rejected an unsupported
//     "done", auto-continued a truncated answer, ran the project's tests,
//     reviewed the diff, or verified real changes.
//   - progress_update SSE events → a docked "Progress" panel (like the task
//     list in Cowork) whose ticks carry a "verified" mark only when a tool
//     actually succeeded between updates.
//   - queue_status SSE events → "in queue" card while the GPU is busy.
//   - Turn summary actions: restore to before this turn (checkpoint), revert
//     (git), commit these changes, review mode accept/reject per file.
// Zero dependencies on chat.js internals: it only appends to #chat-history and
// listens to the custom events below.

let API_BASE = '';
let _progressEl = null;
let _progressCollapsed = false;
let _currentSessionId = null;
let _lastTodosBySession = new Map();
// The cache is only a paint-instantly convenience; keep it from growing with
// every chat the user ever opens in this tab.
const _PROGRESS_CACHE_MAX = 24;
let _filesBySession = new Map();     // sessionId → Map(path → workspace) of files edited in that chat
let _queueCard = null;

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

function _card(kind, title, bodyHtml, { open = false, icon = '🛡' } = {}) {
  const thread = _threadForCard();
  if (!thread) return null;
  const node = document.createElement('div');
  node.className = `agent-thread-node harness-node harness-${kind}${open ? ' expanded' : ''}`;
  node.innerHTML =
    `<div class="agent-thread-dot"></div>` +
    `<div class="agent-thread-header harness-header"><span class="agent-thread-icon">${icon}</span>` +
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
// `checkpoint` makes the viewer diff/revert against the turn's baseline;
// `review` = {msg, state} adds review-mode attributes.
function _fileChip(path, workspace, mode, { checkpoint = null, review = null } = {}) {
  const p = String(path || '');
  const base = p.split(/[\\/]/).pop();
  const attrs = [
    `data-open-file="${esc(p)}"`,
    workspace ? `data-open-workspace="${esc(workspace)}"` : '',
    mode ? `data-open-mode="${mode}"` : '',
    checkpoint ? `data-open-checkpoint="${esc(checkpoint)}"` : '',
    review && review.msg ? `data-review-msg="${esc(review.msg)}"` : '',
    review && review.state ? `data-review-state="${esc(review.state)}"` : '',
  ].filter(Boolean).join(' ');
  const st = review && review.state ? ` is-${review.state}` : (review ? ' is-pending' : '');
  return `<a href="#" class="harness-file${st}" ${attrs} title="${esc(p)} — click to review">${esc(base)}</a>`;
}

/** Files row of a turn (live summary AND restored history): chips, restore /
 *  revert, commit, review-mode controls. `hz` = the harness data of the turn. */
export function turnFilesRowHtml(hz, { messageId = null, reviewState = null } = {}) {
  const files = Array.isArray(hz.mutations) ? hz.mutations : [];
  if (!files.length) return '';
  const ws = hz.workspace || null;
  const cp = hz.checkpoint || null;
  const review = hz.review_mode ? { msg: messageId, state: null } : null;
  const stateOf = (f) => {
    if (!reviewState) return null;
    if ((reviewState.accepted || []).includes(f)) return 'accepted';
    if ((reviewState.rejected || []).includes(f)) return 'rejected';
    return 'pending';
  };
  const chips = files.map(f => _fileChip(f, ws, 'diff', { checkpoint: cp, review: review ? { msg: messageId, state: stateOf(f) } : null })).join(' ');
  const payload = esc(JSON.stringify({ files: files.slice(0, 60), workspace: ws, checkpoint: cp }));
  const actions = [];
  if (cp) {
    actions.push(`<button type="button" class="harness-btn harness-btn-danger" data-restore-turn="${payload}" title="Put every file of this turn back to its state before the turn (checkpoint ${esc(String(cp).slice(0, 10))})">⟲ Restore to before this turn</button>`);
  } else {
    actions.push(`<button type="button" class="harness-btn harness-btn-danger" data-revert-all="${payload}" title="Undo the changes of this turn (git checkout per file; a new untracked file is deleted)">↺ Revert all ${files.length}</button>`);
  }
  actions.push(`<button type="button" class="harness-btn" data-commit-turn="${payload}" title="git commit exactly these files (the message is proposed, you can edit it)">⎘ Commit these changes…</button>`);
  let reviewBar = '';
  if (hz.review_mode) {
    const pending = reviewState ? (reviewState.pending || []).length : files.length;
    reviewBar = `<div class="harness-review-bar" data-review-bar="1"${messageId ? ` data-review-msg="${esc(messageId)}"` : ''}>` +
      `<span class="harness-review-label">Review mode · <b class="harness-review-count">${pending}</b> file${pending === 1 ? '' : 's'} pending</span> ` +
      `<button type="button" class="harness-btn" data-review-all="accept" title="Accept every pending file">✓ Accept all</button> ` +
      `<button type="button" class="harness-btn harness-btn-danger" data-review-all="reject" title="Reject every pending file (restore them)">✗ Reject all</button>` +
      `<span class="harness-muted"> — open a file to accept or reject it individually</span></div>`;
  }
  return `<div class="harness-files-row harness-files" data-turn-files="1"${messageId ? ` data-message-id="${esc(messageId)}"` : ''}${cp ? ` data-checkpoint="${esc(cp)}"` : ''}>${chips}</div>` +
    `<div class="harness-actions">${actions.join(' ')} <span class="harness-commit-slot"></span></div>` + reviewBar;
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

async function _confirm(q, confirmText) {
  try {
    return window.uiModule && window.uiModule.styledConfirm
      ? await window.uiModule.styledConfirm(q, { confirmText, danger: true })
      : window.confirm(q);
  } catch (_) { return window.confirm(q); }
}

function _noteAfter(button, text) {
  const note = document.createElement('div');
  note.className = 'harness-muted harness-revert-result';
  note.textContent = text;
  button.insertAdjacentElement('afterend', note);
}

async function _revertAll(button) {
  let payload;
  try { payload = JSON.parse(button.dataset.revertAll || '{}'); } catch (_) { return; }
  const files = Array.isArray(payload.files) ? payload.files : [];
  if (!files.length) return;
  const ws = payload.workspace || _workspaceFallback();
  const ok = await _confirm(`Revert the ${files.length} file${files.length === 1 ? '' : 's'} changed in this turn? (git checkout — a new untracked file is deleted)`, 'Revert all');
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
  _noteAfter(button, results.map(r => `${r.name}: ${r.action.replace('_', ' ')}`).join(' · '));
  try { if (window.fileViewer && window.fileViewer.isOpen()) window.fileViewer.close(); } catch (_) {}
}

// "Restore to before this turn": every file of the turn back to the checkpoint
// (POST /api/workspace/checkpoint/restore). Works without the user's git.
async function _restoreTurn(button) {
  let payload;
  try { payload = JSON.parse(button.dataset.restoreTurn || '{}'); } catch (_) { return; }
  const files = Array.isArray(payload.files) ? payload.files : [];
  if (!files.length || !payload.checkpoint) return;
  const ws = payload.workspace || _workspaceFallback();
  const ok = await _confirm(`Restore the ${files.length} file${files.length === 1 ? '' : 's'} of this turn to their state before it? Files the turn created are deleted.`, 'Restore');
  if (!ok) return;
  button.disabled = true;
  button.textContent = 'Restoring…';
  try {
    const qs = `workspace=${encodeURIComponent(ws || '')}&sha=${encodeURIComponent(payload.checkpoint)}`;
    const r = await fetch(`${API_BASE}/api/workspace/checkpoint/restore?${qs}`, {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths: files }),
    });
    if (!r.ok) {
      let msg = `HTTP ${r.status}`; try { msg = (await r.json()).detail || msg; } catch (_) {}
      button.textContent = '⟲ Restore failed';
      _noteAfter(button, msg);
      button.disabled = false;
      return;
    }
    const res = await r.json();
    const n = (res.restored || []).length + (res.deleted || []).length;
    button.textContent = `⟲ Restored ${n}/${files.length}`;
    const bits = [];
    if ((res.restored || []).length) bits.push(`restored: ${res.restored.map(p => p.split('/').pop()).join(', ')}`);
    if ((res.deleted || []).length) bits.push(`deleted (new in this turn): ${res.deleted.map(p => p.split('/').pop()).join(', ')}`);
    if ((res.failed || []).length) bits.push(`failed: ${res.failed.join(', ')}`);
    if (res.unchanged) bits.push(`${res.unchanged} already identical`);
    _noteAfter(button, bits.join(' · ') || 'nothing to restore');
    try { if (window.fileViewer && window.fileViewer.isOpen()) window.fileViewer.close(); } catch (_) {}
  } catch (e) {
    button.textContent = '⟲ Restore failed';
    button.disabled = false;
  }
}

// "Commit these changes": ask for a proposed message, show an inline editor,
// then git commit exactly the turn's files (POST /api/workspace/commit).
async function _commitTurn(button) {
  let payload;
  try { payload = JSON.parse(button.dataset.commitTurn || '{}'); } catch (_) { return; }
  const files = Array.isArray(payload.files) ? payload.files : [];
  if (!files.length) return;
  const ws = payload.workspace || _workspaceFallback();
  const slot = button.parentElement && button.parentElement.querySelector('.harness-commit-slot');
  if (!slot) return;
  if (slot.querySelector('.harness-commit-form')) { slot.innerHTML = ''; return; }
  slot.innerHTML = '<span class="harness-muted">Preparing the commit…</span>';
  // The user's request lives in the previous user bubble; use it for the proposal.
  let text = '';
  try {
    const msgs = [...document.querySelectorAll('#chat-history .msg.user, #chat-history .message.user, #chat-history [data-role="user"]')];
    const last = msgs[msgs.length - 1];
    text = last ? (last.innerText || last.textContent || '').trim().slice(0, 400) : '';
  } catch (_) {}
  const lang = /[¿¡ñáéíóú]/i.test(text) ? 'es' : 'en';
  let proposal = { git: false, message: '' };
  try {
    const qs = `workspace=${encodeURIComponent(ws || '')}&paths=${encodeURIComponent(files.join('\n'))}&text=${encodeURIComponent(text)}&language=${lang}`;
    const r = await fetch(`${API_BASE}/api/workspace/commit/proposal?${qs}`, { credentials: 'same-origin' });
    if (r.ok) proposal = await r.json();
  } catch (_) {}
  if (!proposal.git) {
    slot.innerHTML = '<span class="harness-muted">Not a git repository — nothing to commit to. (Restore still works through the checkpoint.)</span>';
    return;
  }
  const form = document.createElement('div');
  form.className = 'harness-commit-form';
  form.innerHTML = `<textarea class="harness-commit-msg" rows="4" spellcheck="false"></textarea>` +
    `<div class="harness-commit-actions"><span class="harness-muted">${files.length} file${files.length === 1 ? '' : 's'} → ${esc(proposal.repo || ws)}</span> ` +
    `<button type="button" class="harness-btn" data-commit-cancel="1">Cancel</button> <button type="button" class="harness-btn harness-btn-primary" data-commit-go="1">Commit</button></div>`;
  form.querySelector('.harness-commit-msg').value = proposal.message || '';
  slot.innerHTML = '';
  slot.appendChild(form);
  form.querySelector('[data-commit-cancel]').addEventListener('click', () => { slot.innerHTML = ''; });
  form.querySelector('[data-commit-go]').addEventListener('click', async () => {
    const msg = form.querySelector('.harness-commit-msg').value.trim();
    const go = form.querySelector('[data-commit-go]');
    go.disabled = true; go.textContent = 'Committing…';
    try {
      const r = await fetch(`${API_BASE}/api/workspace/commit?workspace=${encodeURIComponent(ws || '')}`, {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: files, message: msg }),
      });
      if (!r.ok) {
        let m = `HTTP ${r.status}`; try { m = (await r.json()).detail || m; } catch (_) {}
        slot.innerHTML = `<span class="harness-muted">Commit failed: ${esc(m)}</span>`;
        return;
      }
      const res = await r.json();
      slot.innerHTML = `<span class="harness-muted">✓ Committed ${esc(res.sha || '')} — ${(res.files || []).length} file${(res.files || []).length === 1 ? '' : 's'}</span>`;
      button.disabled = true;
      button.textContent = `⎘ Committed ${res.sha || ''}`;
    } catch (e) {
      slot.innerHTML = `<span class="harness-muted">Commit failed: ${esc(String(e))}</span>`;
    }
  });
  try { form.querySelector('.harness-commit-msg').focus(); } catch (_) {}
}

// Review mode: accept / reject every pending file of the turn.
async function _reviewAll(button) {
  const bar = button.closest('[data-review-bar]');
  const decision = button.dataset.reviewAll;
  if (!bar || !decision) return;
  const msgId = bar.dataset.reviewMsg || _lastAssistantDbId();
  if (!msgId) { _noteAfter(button, 'The turn is still being saved — try again in a second.'); return; }
  let st = null;
  try {
    const r = await fetch(`${API_BASE}/api/workspace/review/${encodeURIComponent(msgId)}`, { credentials: 'same-origin' });
    if (r.ok) st = await r.json();
  } catch (_) {}
  const pending = st ? (st.pending || []) : [];
  if (!pending.length) { _noteAfter(button, 'Nothing pending.'); return; }
  if (decision === 'reject') {
    const ok = await _confirm(`Reject the ${pending.length} pending file${pending.length === 1 ? '' : 's'}? They go back to their state before the turn.`, 'Reject all');
    if (!ok) return;
  }
  button.disabled = true;
  for (const p of pending) {
    try {
      const r = await fetch(`${API_BASE}/api/workspace/review/${encodeURIComponent(msgId)}/decide`, {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: p, decision }),
      });
      if (r.ok) {
        const res = await r.json();
        _applyReviewState(msgId, res.state);
      }
    } catch (_) {}
  }
  button.disabled = false;
  try { if (window.fileViewer && window.fileViewer.isOpen()) window.fileViewer.close(); } catch (_) {}
}

function _lastAssistantDbId() {
  try {
    const els = [...document.querySelectorAll('#chat-history [data-db-id]')];
    for (let i = els.length - 1; i >= 0; i--) {
      if (els[i].dataset.dbId) return els[i].dataset.dbId;
    }
  } catch (_) {}
  return null;
}

/** Paint the review state (accepted/rejected/pending) onto every chip and bar
 *  that belongs to the message. */
function _applyReviewState(msgId, state) {
  if (!msgId || !state) return;
  document.querySelectorAll(`[data-turn-files][data-message-id="${CSS.escape(String(msgId))}"] [data-open-file]`).forEach(chip => {
    const f = chip.dataset.openFile;
    let s = 'pending';
    if ((state.accepted || []).includes(f)) s = 'accepted';
    else if ((state.rejected || []).includes(f)) s = 'rejected';
    chip.classList.remove('is-pending', 'is-accepted', 'is-rejected');
    chip.classList.add(`is-${s}`);
    chip.dataset.reviewState = s;
    chip.dataset.reviewMsg = String(msgId);
  });
  document.querySelectorAll(`[data-review-bar][data-review-msg="${CSS.escape(String(msgId))}"] .harness-review-count`).forEach(c => {
    c.textContent = String((state.pending || []).length);
  });
}

/** Called when the assistant message of a turn gets its database id: bind the
 *  latest unbound files row (live summary rendered before the save) to it and
 *  fetch the review state. */
function _bindMessageId(id) {
  if (!id) return;
  const rows = [...document.querySelectorAll('[data-turn-files]:not([data-message-id])')];
  const row = rows[rows.length - 1];
  if (!row) return;
  row.dataset.messageId = String(id);
  row.querySelectorAll('[data-open-file]').forEach(chip => { if (chip.classList.contains('is-pending')) chip.dataset.reviewMsg = String(id); });
  const bar = row.parentElement && row.parentElement.querySelector('[data-review-bar]:not([data-review-msg])');
  if (bar) bar.dataset.reviewMsg = String(id);
  if (bar) _fetchReviewState(id);
}

async function _fetchReviewState(msgId) {
  try {
    const r = await fetch(`${API_BASE}/api/workspace/review/${encodeURIComponent(msgId)}`, { credentials: 'same-origin' });
    if (r.ok) _applyReviewState(msgId, await r.json());
  } catch (_) {}
}

const REASON_TEXT = {
  claims_without_mutation: 'The model described changes as done, but no write tool (edit_file / write_file / apply_patch) succeeded this turn.',
  fabricated_paths: 'It mentioned files that do not exist in the workspace and were never returned by any tool.',
  claimed_paths_untouched: 'It said it created or modified files that no write tool touched this turn — some of the work it describes did not happen.',
  intent_without_action: 'It announced an action ("I will now…", "Voy a…") and ended the turn without calling any tool.',
};

function _testsLine(t) {
  if (!t || !t.ran) return '';
  const label = t.label || t.kind || 'tests';
  const scope = t.scope === 'related' && Array.isArray(t.related_files) && t.related_files.length
    ? ` (related: ${t.related_files.map(f => `<code>${esc(f.split('/').pop())}</code>`).join(', ')})` : '';
  if (t.inconclusive) return `<div class="harness-foot harness-tests is-inconclusive">⚠ Tests inconclusive — ${esc(t.summary || 'could not run')}${scope} · <code>${esc(label)}</code></div>`;
  if (t.ok) return `<div class="harness-foot harness-tests is-ok">✓ Project tests passed: ${esc(t.summary || 'ok')}${scope} · <code>${esc(label)}</code> · ${esc(String(t.duration_s || 0))}s</div>`;
  const pre = new Set(t.pre_existing || []);
  const fails = (t.failures || []).slice(0, 6).map(f => `<li><code>${esc(f)}</code>${pre.has(f) ? ' <span class="harness-muted">(pre-existing: failed before this change too)</span>' : ''}</li>`).join('');
  if (t.pre_existing_only) {
    return `<div class="harness-foot harness-tests is-warn">⚠ Project tests failing, but they failed <b>before this change too</b> (checked against the checkpoint): ${esc(t.summary || 'failed')}${scope} · <code>${esc(label)}</code></div>` +
      (fails ? `<ul class="harness-list">${fails}</ul>` : '');
  }
  return `<div class="harness-foot harness-tests is-fail">✗ Project tests FAILED: ${esc(t.summary || 'failed')}${scope} · <code>${esc(label)}</code></div>` +
    (fails ? `<ul class="harness-list">${fails}</ul>` : '') +
    (t.output_tail ? `<details class="harness-details"><summary>Output</summary><pre class="harness-pre">${esc(t.output_tail)}</pre></details>` : '');
}

function _reviewLine(r) {
  if (!r || !r.verdict || r.verdict === 'skipped') return '';
  if (r.verdict === 'error') return `<div class="harness-foot harness-review is-inconclusive">⚠ Review could not run (${esc(r.model || '')}): ${esc(r.error || '')}</div>`;
  if (r.verdict === 'unparsed') return `<div class="harness-foot harness-review is-inconclusive">⚠ Reviewer (${esc(r.model || '')}) answered without a verdict: <em>${esc((r.summary || '').slice(0, 200))}</em></div>`;
  const findings = Array.isArray(r.findings) ? r.findings : [];
  const errs = findings.filter(f => f.severity === 'error').length;
  const items = findings.slice(0, 8).map(f => `<li class="is-${esc(f.severity || 'warning')}"><b>${esc(f.severity || 'warning')}</b> <code>${esc(f.file || '?')}${f.line ? ':' + esc(f.line) : ''}</code> — ${esc(f.issue || '')}</li>`).join('');
  if (r.verdict === 'ok' && !findings.length) return `<div class="harness-foot harness-review is-ok">✓ Independent review (${esc(r.model || '')}): no obvious defects${r.summary ? ` — <em>${esc(r.summary)}</em>` : ''} · ${esc(String(r.duration_s || 0))}s</div>`;
  const disputed = r.disputed ? ' · <b>the agent checked and disagreed</b> (nothing changed — see its answer)' : '';
  const ungrounded = r.ungrounded ? ` · ${r.ungrounded} not located in the diff` : '';
  return `<div class="harness-foot harness-review ${errs && !r.disputed ? 'is-fail' : 'is-warn'}">${errs && !r.disputed ? '✗' : '⚠'} Independent review (${esc(r.model || '')}): ${findings.length} finding${findings.length === 1 ? '' : 's'}${errs ? ` (${errs} likely defect${errs === 1 ? '' : 's'})` : ''}${r.summary ? ` — <em>${esc(r.summary)}</em>` : ''}${disputed}${ungrounded}</div>` +
    (items ? `<ul class="harness-list harness-findings">${items}</ul>` : '');
}

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
  if (status === 'checkpoint') {
    _card('checkpoint', `Checkpoint taken before the first change of this turn${json.ms != null ? ` · ${json.ms} ms` : ''}${json.created === false ? ' (workspace unchanged since the last one)' : ''}`,
      `<div class="harness-foot">Shadow snapshot <code>${esc(String(json.sha || '').slice(0, 10))}</code> — "Restore to before this turn" and per-file diffs use it, with or without git.</div>`, { icon: '⟲' });
    return;
  }
  if (status === 'tests_running') {
    _card('running', `Running the project's tests${json.label ? ` (${json.label})` : ''}…`, '', { icon: '🧪' });
    return;
  }
  if (status === 'tests_failed') {
    _card('rejected', `Project tests failed after the changes — asked the model to fix the cause (${json.attempt}/${json.max_attempts})`, _testsLine(json.tests), { open: true, icon: '🧪' });
    return;
  }
  if (status === 'review_running') {
    _card('running', `Reviewing the diff with ${json.model || 'a second pass'}…`, '', { icon: '🔍' });
    return;
  }
  if (status === 'review_issues') {
    _card('rejected', `Independent review flagged likely defects — asked the model to verify and fix them (${json.attempt}/${json.max_attempts})`, _reviewLine(json.review), { open: true, icon: '🔍' });
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
  if (status === 'target_substituted') {
    const missing = (json.missing || []).map(p => `<code>${esc(p)}</code>`).join(', ');
    const changed = (json.changed || []).map(p => `<code>${esc(p)}</code>`).join(', ');
    _card('rejected',
      `You named ${(json.missing || []).join(', ')} — it does not exist; the model changed other files without saying so. Asked for an explicit answer (or a question)`,
      `<div class="harness-foot">Named by you: ${missing || '—'} · changed instead: ${changed || '—'}. The edits stay; review them with the chips below or revert.</div>`, { open: true });
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
  if (json.untouched_paths && json.untouched_paths.length) {
    items.push(`<li>Claimed ${json.untouched_paths.map(p => `<code>${esc(p)}</code>`).join(', ')} but never touched ${json.untouched_paths.length === 1 ? 'it' : 'them'}.</li>`);
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
    const t = json.tests;
    const rv = json.review;
    const body = [];
    if (files.length) body.push(`<div class="harness-foot harness-files">${files.map(f => _fileChip(f, json.workspace, 'diff', { checkpoint: json.checkpoint || null })).join(' ')}</div>`);
    if (checked) body.push(`<div class="harness-foot">Syntax check passed: ${checks.filter(c => c.ok).map(c => `<code>${esc(c.path)}</code>`).join(' ')}</div>`);
    body.push(_testsLine(t));
    body.push(_reviewLine(rv));
    const bits = [];
    if (files.length) bits.push(`${files.length} file${files.length === 1 ? '' : 's'} changed`);
    if (checked) bits.push(`${checked} syntax-checked`);
    if (t && t.ran) bits.push(t.inconclusive ? 'tests inconclusive' : (t.ok ? 'tests passed' : 'tests FAILED'));
    if (rv && rv.verdict && rv.verdict !== 'skipped') bits.push(rv.verdict === 'ok' ? 'review ok' : (rv.verdict === 'issues' ? `review: ${(rv.findings || []).length} finding${(rv.findings || []).length === 1 ? '' : 's'}` : 'review n/a'));
    const bad = (t && t.ran && t.ok === false && !t.inconclusive) || (rv && rv.verdict === 'issues' && (rv.findings || []).some(f => f.severity === 'error'));
    _card(bad ? 'unverified' : 'verified',
      files.length ? `${bad ? 'Changed but NOT green' : 'Verified'}: ${bits.join(' · ')}` : 'Verified against the tool log',
      body.join(''), { open: !!bad });
  }
}

export function renderHarnessSummary(json, { messageId = null } = {}) {
  const d = json.data || {};
  const tools = d.tool_calls || 0;
  const failed = d.failed_calls || 0;
  const files = d.mutations || [];
  const git = d.git;
  const stop = d.stop_reason || 'complete';
  const parts = [];
  // A turn parked at the approval gate counts its sealed call as "failed"
  // in the ledger (it did not run). To the user it is not a failure — it is
  // the call they are being asked about.
  const gated = stop === 'awaiting_user' && failed > 0 ? Math.min(failed, 1) : 0;
  const reallyFailed = failed - gated;
  parts.push(`${tools} tool call${tools === 1 ? '' : 's'}${reallyFailed ? ` (${reallyFailed} failed)` : ''}${gated ? ' (1 awaiting approval)' : ''}`);
  parts.push(files.length ? `Edited ${files.length} file${files.length === 1 ? '' : 's'}` : 'no files changed');
  if (git && typeof git.changed_count === 'number') {
    parts.push(git.changed_count ? `git: ${git.changed_count} path${git.changed_count === 1 ? '' : 's'} dirty${git.shortstat ? ` (${git.shortstat.trim()})` : ''}` : 'git: clean');
  }
  if (d.tests && d.tests.ran) parts.push(d.tests.inconclusive ? 'tests: inconclusive' : (d.tests.ok ? 'tests: ✓' : 'tests: ✗'));
  if (d.review && d.review.verdict === 'ok') parts.push('review: ✓');
  else if (d.review && d.review.verdict === 'issues') parts.push(`review: ${(d.review.findings || []).length} finding${(d.review.findings || []).length === 1 ? '' : 's'}`);
  if (d.rejections) parts.push(`${d.rejections} rejection${d.rejections === 1 ? '' : 's'}`);
  if (d.length_continues) parts.push(`${d.length_continues} auto-continue`);
  const stopLabel = {
    complete: 'finished', complete_unverified: 'finished — UNVERIFIED', rounds_exhausted: 'step limit',
    budget_exceeded: 'tool budget', loop_breaker: 'loop breaker', intent_nudge_exhausted: 'stalled',
    awaiting_user: 'waiting for you', length: 'cut off',
  }[stop] || stop;
  const details = [];
  if (files.length) {
    details.push(`<div class="harness-files-head"><b>Edited:</b> <span class="harness-muted">click a file to review it (diff vs. before this turn / contents / open folder)</span></div>`);
    details.push(turnFilesRowHtml({ mutations: files, workspace: d.workspace, checkpoint: d.checkpoint, review_mode: d.review_mode }, { messageId }));
  }
  details.push(_testsLine(d.tests));
  details.push(_reviewLine(d.review));
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
    target_substituted: p => `you named <code>${esc(p)}</code> (does not exist) — the model changed other files; it was asked to say so explicitly`,
    tests_failed: s => `<b>⚠ the project's tests still fail</b> after the fix round: ${esc(s)}`,
    tests_pre_existing: s => `the project's tests fail, but they already failed before this change (checked at the checkpoint): ${esc(s)}`,
    review_defects: n => `<b>⚠ the reviewer still sees ${esc(n)} likely defect${String(n) === '1' ? '' : 's'}</b> after the fix round — check the findings above`,
    review_disputed: n => `the reviewer flagged ${esc(n)} point${String(n) === '1' ? '' : 's'}; the agent checked them and disagreed (nothing changed) — its answer says why`,
  };
  const noteLines = notes.map(n => {
    const [k, ...rest] = String(n).split(/[:@]/);
    const fn = NOTE_TEXT[k];
    return fn ? fn(rest.join(':')) : esc(String(n));
  });
  if (noteLines.length) details.push(`<div><b>Notes:</b><ul class="harness-list">${noteLines.map(l => `<li>${l}</li>`).join('')}</ul></div>`);
  const rewrote = notes.some(n => String(n).startsWith('whole_file_rewrite:'));
  if (rewrote) parts.push('⚠ whole-file rewrite');
  const red = stop === 'complete_unverified' || notes.some(n => /^(tests_failed|review_defects)/.test(String(n)));
  const kind = red ? 'unverified' : (files.length ? 'verified' : 'summary');
  _card(kind, `Turn summary · ${parts.join(' · ')} · ${stopLabel}`, details.join(''), { open: files.length > 0 });
}

// ── Queue (task queue for the local GPU) ────────────────────────────────────

export function renderQueueStatus(json) {
  if (json.queued) {
    const ahead = Array.isArray(json.ahead) && json.ahead.length ? ` · ahead: ${json.ahead.map(esc).join(', ')}` : '';
    const title = `In queue — position ${json.position}${json.active ? ` (${json.active} running)` : ''}`;
    if (_queueCard && document.body.contains(_queueCard)) {
      _queueCard.querySelector('.agent-thread-tool').textContent = title;
      const body = _queueCard.querySelector('.harness-body');
      if (body) body.innerHTML = `<div class="harness-foot">Waiting for the ${esc(json.lane || 'local')} lane (one generation at a time on the GPU). It starts on its own; you can close the tab.${ahead}</div>`;
      return;
    }
    _queueCard = _card('queued', title, `<div class="harness-foot">Waiting for the ${esc(json.lane || 'local')} lane (one generation at a time on the GPU). It starts on its own; you can close the tab.${ahead}</div>`, { open: true, icon: '⏳' });
    return;
  }
  if (_queueCard && document.body.contains(_queueCard)) {
    _queueCard.querySelector('.agent-thread-tool').textContent = 'Started (the queue reached this chat)';
    _queueCard.classList.remove('expanded');
    _queueCard.classList.remove('harness-queued');
    _queueCard.classList.add('harness-checkpoint');
  }
  _queueCard = null;
}

// ── Sub-agent board v3 (delegate_agents) ─────────────────────────────────────
// One CARD per worker (Cowork / Claude-Code style), fed by `tool_progress`
// events that carry a `subagent` payload (src/agent_tools/subagent_tools.py):
//   queued · started · round · tool (start/progress/done) · tick (~5 s:
//   elapsed, idle, tokens, stalled) · steer · supervisor · harness · guard ·
//   error · done.
// Every field is optional (older backends send a subset) — the card degrades
// to what it knows. State is kept PER PARENT CHAT so events that arrive while
// that chat is in the background are not lost: chat.js keeps calling
// renderSubagentEvent() with `background: true`, and restoreSubagentBoard()
// repaints the cards when the user comes back (chat.js checkBackgroundStream).

const _SA_MAX_SESSIONS = 12;
const _SA_NO_SIGNAL_MS = 20000;    // ticks stopped arriving (only judged once a tick was seen)
let _saBoards = new Map();         // parent sessionId → { workers: Map(id → state), order: [] }
let _saTicker = null;              // 1 s interval: elapsed, stalled seconds, Re-run enablement

function _toast(msg) {
  try {
    if (window.uiModule && typeof window.uiModule.showToast === 'function') { window.uiModule.showToast(msg); return; }
  } catch (_) {}
  try { console.info(msg); } catch (_) {}
}

function _boardFor({ trailing = false } = {}) {
  const chatBox = document.getElementById('chat-history');
  if (!chatBox) return null;
  // The delegate_agents tool card is the last running node in the thread;
  // attach the board right after it so workers appear where the call is.
  // `trailing` (repaint after a history render, where the last thread is an
  // OLDER turn's): only reuse a thread that ends the timeline, else open one
  // after the user's message.
  const threads = chatBox.querySelectorAll('.agent-thread');
  const thread = trailing ? _threadForCard() : (threads.length ? threads[threads.length - 1] : _threadForCard());
  if (!thread) return null;
  let board = thread.querySelector('.subagent-board:last-of-type');
  if (board && board.dataset.open === '1') return board;
  // Built with createElement (not one innerHTML) so the node-based tests can
  // drive it with a minimal DOM stub.
  board = document.createElement('div');
  board.className = 'agent-thread-node harness-node harness-subagents expanded subagent-board subagent-board-v3';
  board.dataset.open = '1';
  const dot = document.createElement('div');
  dot.className = 'agent-thread-dot';
  board.appendChild(dot);
  const header = document.createElement('div');
  header.className = 'agent-thread-header harness-header';
  header.innerHTML = `<span class="agent-thread-icon">🤖</span><span class="agent-thread-tool">Sub-agents</span>`;
  const count = document.createElement('span');
  count.className = 'subagent-board-count';
  header.appendChild(count);
  const summary = document.createElement('span');
  summary.className = 'subagent-board-summary harness-muted';
  header.appendChild(summary);
  board.appendChild(header);
  const body = document.createElement('div');
  body.className = 'agent-thread-content harness-body';
  const rows = document.createElement('div');
  rows.className = 'subagent-rows subagent-cards';
  body.appendChild(rows);
  board.appendChild(body);
  thread.appendChild(board);
  return board;
}

function _saState(sessionId, create) {
  const key = sessionId || '__current__';
  let st = _saBoards.get(key);
  if (!st && create) {
    st = { workers: new Map(), order: [] };
    _saBoards.set(key, st);
    while (_saBoards.size > _SA_MAX_SESSIONS) {
      const oldest = _saBoards.keys().next().value;
      if (oldest === undefined) break;
      _saBoards.delete(oldest);
    }
  } else if (st && create) {
    _saBoards.delete(key);          // re-insert => most recent last (LRU)
    _saBoards.set(key, st);
  }
  return st || null;
}

function _saNewWorker(id, now) {
  return {
    id, index: null, name: '', role: 'worker', model: '', files: [], instruction: '', instructionFull: '',
    sessionId: '', status: 'running', firstSeen: now, startedLocal: null, startedAt: null, endedAt: null,
    lastEventAt: now, sawTick: false, tickElapsed: null, tickAt: null, round: null, maxRounds: null, rounds: null,
    toolCalls: 0, failedCalls: 0, lastTool: '', lastCmd: '', lastToolOk: null, lastOut: '', tail: '', toolElapsed: null, toolInFlight: false,
    inTok: null, outTok: null, idleS: null, stalled: false, stallReason: '', stallAt: null,
    stallIdleAt: null, timeoutS: null, endedLocal: null,
    steers: [], supervisor: [], note: '', error: '', stopReason: '', finalText: '', mutations: [], durationS: null,
    stopRequested: false,
  };
}

/** Live event → state (+ card when the parent chat is on screen).
 *  `sessionId` = the PARENT chat (defaults to the chat on screen);
 *  `background` = record only, chat.js paints nothing for background chats. */
export function renderSubagentEvent(json, { sessionId = null, background = false } = {}) {
  const sa = json.subagent || {};
  const id = sa.id != null ? String(sa.id) : (sa.session_id ? String(sa.session_id) : '');
  if (!id) return;
  const now = Date.now();
  const parent = sessionId || _currentSessionId || null;
  const st = _saState(parent, true);
  let w = st.workers.get(id);
  if (!w) { w = _saNewWorker(id, now); st.workers.set(id, w); st.order.push(id); }
  _saApply(w, sa, now);
  if (background || (parent && _currentSessionId && parent !== _currentSessionId)) return;
  const board = _boardFor();
  if (!board) return;
  const row = _saPaintWorker(board, w);
  _saPaintHeader(board, st);
  _saEnsureTicker();
  try { if (row && row.scrollIntoView) row.scrollIntoView({ block: 'nearest' }); } catch (_) {}
}

/** Repaint the cards of a chat from the retained state (the user came back
 *  to a chat whose delegation ran in the background). Returns true when a
 *  board was painted. */
export function restoreSubagentBoard(sessionId) {
  const st = _saState(sessionId || _currentSessionId, false);
  if (!st || !st.order.length) return false;
  const board = _boardFor({ trailing: true });
  if (!board) return false;
  for (const id of st.order) {
    const w = st.workers.get(id);
    if (w) _saPaintWorker(board, w);
  }
  _saPaintHeader(board, st);
  _saEnsureTicker();
  return true;
}

/** Retained worker states of a chat (for tests / other UI). */
export function subagentBoardState(sessionId) {
  const st = _saState(sessionId || _currentSessionId, false);
  return st ? st.order.map(id => st.workers.get(id)).filter(Boolean) : [];
}

const _SA_LIVE = { queued: 1, running: 1 };
function _saLive(w) { return !!_SA_LIVE[w.status]; }

/** Fold one subagent payload into the worker state (pure; no DOM). */
function _saApply(w, sa, now) {
  w.lastEventAt = now;
  if (sa.name) w.name = String(sa.name);
  if (sa.role) w.role = String(sa.role);
  if (sa.index != null) w.index = Number(sa.index);
  if (sa.session_id) w.sessionId = String(sa.session_id);
  if (sa.model) w.model = String(sa.model);
  if (Array.isArray(sa.files)) w.files = sa.files.map(String);
  if (sa.instruction) w.instruction = String(sa.instruction);
  if (sa.instruction_full) w.instructionFull = String(sa.instruction_full);
  if (sa.max_rounds != null) w.maxRounds = Number(sa.max_rounds);
  if (sa.timeout_s != null) w.timeoutS = Number(sa.timeout_s);
  if (sa.started_at != null && Number(sa.started_at) > 0) w.startedAt = Number(sa.started_at);
  if (sa.ended_at != null && Number(sa.ended_at) > 0) w.endedAt = Number(sa.ended_at);
  if (sa.input_tokens != null) w.inTok = Number(sa.input_tokens) || 0;
  if (sa.output_tokens != null) w.outTok = Number(sa.output_tokens) || 0;
  if (sa.rounds != null) w.rounds = Number(sa.rounds);
  switch (sa.event) {
    case 'queued':
      w.status = 'queued';
      w.note = sa.reason ? String(sa.reason) : 'waiting for a GPU slot';
      break;
    case 'started':
      w.status = 'running';
      w.startedLocal = now;
      w.note = '';
      break;
    case 'round':
      if (sa.round != null) w.round = Number(sa.round);
      w.stalled = false;
      break;
    case 'tool':
      w.stalled = false;
      if (sa.tool) w.lastTool = String(sa.tool);
      if (sa.phase === 'start') {
        w.lastCmd = String(sa.command || '');
        w.lastToolOk = null;
        w.lastOut = '';
        w.tail = '';
        w.toolElapsed = null;
        w.toolInFlight = true;
      } else if (sa.phase === 'progress') {
        w.toolInFlight = true;
        if (sa.tail != null) w.tail = String(sa.tail);
        if (sa.elapsed_s != null) w.toolElapsed = Number(sa.elapsed_s);
      } else {
        w.toolInFlight = false;
        w.toolCalls += 1;
        if (sa.ok === false) w.failedCalls += 1;
        w.lastToolOk = sa.ok !== false;
        w.lastOut = String(sa.output || '');
        w.tail = '';
        w.toolElapsed = null;
      }
      break;
    case 'tick':
      w.sawTick = true;
      if (sa.elapsed_s != null) { w.tickElapsed = Number(sa.elapsed_s); w.tickAt = now; }
      if (sa.round != null) w.round = Number(sa.round);
      if (sa.last_tool) w.lastTool = String(sa.last_tool);
      if (sa.tool_calls != null) w.toolCalls = Math.max(w.toolCalls, Number(sa.tool_calls) || 0);
      if (sa.idle_s != null) w.idleS = Number(sa.idle_s);
      if (sa.stalled) {
        if (!w.stalled) w.stallAt = now;
        w.stalled = true;
        w.stallReason = String(sa.stall_reason || '');
        w.stallIdleAt = now;
      } else {
        w.stalled = false;
      }
      break;
    case 'steer': {
      const text = String(sa.text || '');
      const source = String(sa.source || 'user');
      // The Steer… button paints its line right away; the server echoes it.
      const last = w.steers[w.steers.length - 1];
      if (!(last && last.text === text && last.source === source && last.local && now - last.at < 60000)) {
        w.steers.push({ text, source, at: now });
      } else { last.local = false; }
      break;
    }
    case 'supervisor':
      w.supervisor.push({ action: String(sa.action || ''), reason: String(sa.reason || '') });
      break;
    case 'harness':
      w.note = `🛡 ${sa.status || ''}${sa.reasons && sa.reasons.length ? ': ' + sa.reasons.join(', ') : ''}`;
      break;
    case 'guard':
      w.note = `⚠ ${sa.kind || 'guard'}`;
      break;
    case 'error':
      w.status = 'failed';
      w.error = String(sa.message || 'error');
      break;
    case 'done': {
      const stopped = sa.stop_reason === 'stopped';
      const ok = !sa.error && sa.stop_reason === 'complete';
      w.status = sa.error ? 'failed' : (ok ? 'done' : (stopped ? 'stopped' : 'partial'));
      w.stopReason = String(sa.stop_reason || '');
      w.error = sa.error ? String(sa.error) : w.error;
      w.finalText = String(sa.final_text || '');
      w.mutations = Array.isArray(sa.mutations) ? sa.mutations.map(String) : w.mutations;
      if (sa.tool_calls != null) w.toolCalls = Number(sa.tool_calls) || 0;
      if (sa.failed_calls != null) w.failedCalls = Number(sa.failed_calls) || 0;
      if (sa.duration_s != null) w.durationS = Number(sa.duration_s);
      if (!w.endedAt) w.endedLocal = now;
      w.stalled = false;
      w.tail = '';
      w.toolInFlight = false;
      if (Array.isArray(sa.steered)) {
        for (const s of sa.steered) {
          const text = typeof s === 'string' ? s : String((s && s.text) || '');
          const source = typeof s === 'string' ? 'user' : String((s && s.source) || 'user');
          if (text && !w.steers.some(x => x.text === text)) w.steers.push({ text, source, at: now });
        }
      }
      if (Array.isArray(sa.supervisor) && !w.supervisor.length) {
        w.supervisor = sa.supervisor.map(x => (typeof x === 'string' ? { action: x, reason: '' } : { action: String((x && x.action) || ''), reason: String((x && x.reason) || '') }));
      }
      break;
    }
    default:
      break;
  }
  return w;
}

/** Seconds this worker has been (or was) running. */
function _saElapsed(w, now) {
  if (w.startedAt && w.endedAt) return Math.max(0, w.endedAt - w.startedAt);
  if (!_saLive(w)) {
    if (w.durationS != null) return w.durationS;
    if (w.startedAt && w.endedLocal) return Math.max(0, w.endedLocal / 1000 - w.startedAt);
    if (w.startedLocal && w.endedLocal) return Math.max(0, (w.endedLocal - w.startedLocal) / 1000);
  }
  // The tick's own elapsed beats the wall clock: it does not care about the
  // skew between the server's and the browser's clocks.
  if (w.tickElapsed != null && w.tickAt) return w.tickElapsed + Math.max(0, now - w.tickAt) / 1000;
  if (w.startedAt) return Math.max(0, now / 1000 - w.startedAt);
  return Math.max(0, (now - (w.startedLocal || w.firstSeen)) / 1000);
}

function _fmtDur(s) {
  const v = Math.max(0, Math.round(Number(s) || 0));
  if (v < 60) return `${v}s`;
  if (v < 3600) return `${Math.floor(v / 60)}m ${String(v % 60).padStart(2, '0')}s`;
  return `${Math.floor(v / 3600)}h ${String(Math.floor((v % 3600) / 60)).padStart(2, '0')}m`;
}

/** Status pill: {kind, text} — `kind` is also the CSS modifier. */
function _saPill(w, now) {
  if (w.status === 'queued') return { kind: 'queued', text: 'queued' };
  if (w.status === 'running') {
    if (w.stalled) {
      if (/loop/i.test(w.stallReason)) return { kind: 'stalled', text: 'loop' };
      if (w.idleS != null) {
        const idle = w.idleS + Math.max(0, now - (w.stallIdleAt || now)) / 1000;
        return { kind: 'stalled', text: `no activity ${Math.round(idle)}s` };
      }
      return { kind: 'stalled', text: w.stallReason || 'stalled' };
    }
    if (w.sawTick && now - w.lastEventAt > _SA_NO_SIGNAL_MS) {
      return { kind: 'stalled', text: `no signal ${Math.round((now - w.lastEventAt) / 1000)}s` };
    }
    return { kind: 'running', text: 'running' };
  }
  if (w.status === 'done') return { kind: 'done', text: 'done' };
  if (w.status === 'stopped') return { kind: 'stopped', text: 'stopped' };
  if (w.status === 'failed') return { kind: 'failed', text: 'failed' };
  return { kind: 'partial', text: w.stopReason || 'partial' };
}

/** Cowork-style activity verb ("Editing files", "Running command", …) derived
 *  from what the worker is doing right now; '' when the card is not live. */
const _SA_ACTIVITY = [
  [/^(read_file|ls|list_files|glob|grep|search_files|find_files|read_plan|project_context|repo_map|code_refs)$/, 'Reading files'],
  [/^(edit_file|write_file|apply_patch|replace_across_files|create_file|multi_edit)$/, 'Editing files'],
  [/^(bash|python|run_tests|shell|execute|subprocess)$/, 'Running command'],
  [/^(web_search|web_fetch|fetch_url|mcp__builtin_browser__|browser_)/, 'Browsing'],
  [/^desktop_/, 'Using the desktop'],
  [/^delegate_agents$/, 'Delegating'],
  [/^(ask_user|update_plan|todowrite|save_todos)$/, 'Waiting for you'],
  [/^(manage_skills|memory|remember|recall)/, 'Using memory'],
];
export function subagentActivity(w) {
  if (!w) return '';
  if (w.status === 'queued') return 'Queued';
  if (!_saLive(w)) return '';
  if (w.stalled) return /loop/i.test(w.stallReason || '') ? 'Looping' : 'Idle';
  if (w.toolInFlight && w.lastTool) {
    for (const [re, label] of _SA_ACTIVITY) if (re.test(w.lastTool)) return label;
    return `Using ${w.lastTool}`;
  }
  return 'Thinking';
}

const _SA_ICON = { queued: '⏳', running: '◉', done: '✓', failed: '✗', stopped: '■', partial: '◑' };
const _SA_ROW_CLASS = { queued: 'is-queued', running: 'is-running', done: 'is-done', failed: 'is-error', stopped: 'is-partial is-stopped', partial: 'is-partial' };

function _saTokens(w) {
  if (w.inTok == null && w.outTok == null) return '';
  return `${_fmtTok(w.inTok || 0)} in · ${_fmtTok(w.outTok || 0)} out`;
}

/** Is the parent chat (the one on screen) still streaming? Re-run must wait. */
function _parentStreaming() {
  try {
    const cm = window.chatModule;
    return !!(cm && typeof cm.hasActiveStream === 'function' && _currentSessionId && cm.hasActiveStream(_currentSessionId));
  } catch (_) { return false; }
}

function _rerunButtonHtml(w, streaming) {
  const instruction = w.instructionFull || w.instruction || '';
  if (!instruction) return '';
  const task = { name: w.name, instruction, files: w.files || [], model: w.model || '' };
  const dis = streaming ? ' disabled title="wait for the delegation to finish"' : ' title="Delegate this task again (optionally with another model)"';
  return `<button type="button" class="harness-btn harness-btn-mini" data-rerun-worker="${esc(JSON.stringify(task))}"${dis}>↻ Re-run…</button>`;
}

/** Inner HTML of one worker card. `live` = the board is on a running turn
 *  (Stop / Steer make sense); a restored board only gets Open chat / Re-run. */
function _saCardHtml(w, { live = true, streaming = false, now = Date.now() } = {}) {
  const isReviewer = w.role === 'reviewer';
  const name = isReviewer ? `🔍 ${w.name || 'reviewer'}` : `${(w.index ?? 0) + 1}. ${w.name || 'worker'}`;
  const pill = _saPill(w, now);
  const alive = _saLive(w);
  const head =
    `<div class="subagent-head">` +
    `<span class="subagent-icon">${_SA_ICON[w.status] || '◉'}</span>` +
    `<span class="subagent-name">${esc(name)}</span>` +
    `<span class="subagent-role-badge is-${esc(w.role || 'worker')}">${esc(w.role || 'worker')}</span>` +
    (w.model ? `<span class="subagent-model harness-muted" title="model">${esc(w.model)}</span>` : '') +
    `<span class="subagent-pill is-${pill.kind}">${esc(pill.text)}</span>` +
    (alive ? `<span class="subagent-activity" title="what the worker is doing right now">${esc(subagentActivity(w))}</span>` : '') +
    `</div>`;
  const instruction = w.instruction || w.instructionFull;
  const instr = instruction ? `<div class="subagent-instruction" title="${esc(w.instructionFull || w.instruction)}">${esc(instruction)}</div>` : '';
  const stats = [];
  const started = w.startedAt ? Math.round(w.startedAt * 1000) : (w.startedLocal || w.firstSeen);
  stats.push(`<span class="subagent-elapsed" data-started="${started}" title="elapsed">${_fmtDur(_saElapsed(w, now))}</span>`);
  if (w.round != null || w.rounds != null) {
    const r = alive ? w.round : (w.rounds != null ? w.rounds : w.round);
    stats.push(`<span class="subagent-round" title="round">r${esc(String(r))}${w.maxRounds ? `/${esc(String(w.maxRounds))}` : ''}</span>`);
  }
  stats.push(`<span class="subagent-tools">${w.toolCalls} tool${w.toolCalls === 1 ? '' : 's'}${w.failedCalls ? ` (${w.failedCalls} failed)` : ''}</span>`);
  const tok = _saTokens(w);
  if (tok) stats.push(`<span class="subagent-tokens" title="tokens">${esc(tok)}</span>`);
  if (!alive && w.mutations.length) stats.push(`<span class="subagent-changed">${w.mutations.length} file${w.mutations.length === 1 ? '' : 's'} changed</span>`);
  else if (!alive && w.status !== 'queued') stats.push(`<span class="subagent-changed harness-muted">no files changed</span>`);
  const statsHtml = `<div class="subagent-stats">${stats.join('<span class="subagent-sep"> · </span>')}</div>`;
  // Last line: tool in flight / last tool / final text / error.
  let last = '';
  if (w.error) last = `✗ ${w.error}`;
  else if (!alive && w.finalText) last = w.finalText;
  else if (w.lastTool) {
    const mark = w.toolInFlight ? '▶' : (w.lastToolOk === null ? '·' : (w.lastToolOk ? '✓' : '✗'));
    const cmd = w.toolInFlight ? w.lastCmd : (w.lastCmd || w.lastOut);
    const el = w.toolInFlight && w.toolElapsed != null ? ` (${_fmtDur(w.toolElapsed)})` : '';
    last = `${mark} ${w.lastTool}${el} ${cmd}`.trim();
  } else if (w.note) last = w.note;
  const lastHtml = last ? `<div class="subagent-last" title="${esc(last)}">${esc(last)}</div>` : '';
  const tailHtml = alive && w.tail ? `<pre class="subagent-tail">${esc(w.tail)}</pre>` : '';
  const noteHtml = (w.note && last !== w.note) ? `<div class="subagent-note harness-muted">${esc(w.note)}</div>` : '';
  let filesHtml = '';
  if (w.files.length) {
    filesHtml = `<div class="subagent-files harness-muted">owns ${w.files.map(f => `<code class="subagent-file" title="${esc(f)}">${esc(String(f).split(/[\\/]/).pop())}</code>`).join(' ')}</div>`;
  }
  if (!alive && w.mutations.length) {
    filesHtml += `<div class="subagent-files subagent-mutations harness-files">${w.mutations.slice(0, 40).map(f => _fileChip(f, null, 'diff')).join(' ')}</div>`;
  }
  const lines = [];
  for (const s of w.steers) lines.push(`<div class="subagent-steer">→ steered${s.source && s.source !== 'user' ? ` (${esc(s.source)})` : ''}: ${esc(s.text)}</div>`);
  for (const s of w.supervisor) {
    const verb = s.action === 'nudge' ? 'nudged' : (s.action === 'stop' ? 'stopped' : (s.action || 'acted'));
    lines.push(`<div class="subagent-supervisor">supervisor: ${esc(verb)}${s.reason ? ` — ${esc(s.reason)}` : ''}</div>`);
  }
  const linesHtml = lines.length ? `<div class="subagent-lines">${lines.join('')}</div>` : '';
  // Footer buttons.
  const btns = [];
  if (live && alive && w.sessionId) {
    btns.push(w.stopRequested
      ? `<button type="button" class="harness-btn harness-btn-mini harness-btn-danger" data-stop-worker="${esc(w.sessionId)}" disabled>Stopping…</button>`
      : `<button type="button" class="harness-btn harness-btn-mini harness-btn-danger" data-stop-worker="${esc(w.sessionId)}" title="Stop this worker only">■ Stop</button>`);
    btns.push(`<button type="button" class="harness-btn harness-btn-mini" data-steer-worker="${esc(w.sessionId)}" title="Send this worker a message (injected before its next round)">✎ Steer…</button>`);
  }
  if (w.sessionId) btns.push(`<a href="#${esc(w.sessionId)}" class="harness-btn harness-btn-mini subagent-chat-link" title="Open this worker's chat (${esc(w.sessionId)})">↗ Open chat</a>`);
  const ok = w.status === 'done';
  if (!alive && !ok && !isReviewer) btns.push(_rerunButtonHtml(w, streaming));
  const foot = btns.length ? `<div class="subagent-foot subagent-actions">${btns.join(' ')}</div>` : '';
  return head + instr + statsHtml + lastHtml + tailHtml + noteHtml + filesHtml + linesHtml + foot;
}

function _saPaintWorker(board, w, { live = true } = {}) {
  const rows = board.querySelector('.subagent-rows');
  if (!rows) return null;
  let row = rows.querySelector(`[data-sa="${w.id}"]`);
  if (!row) {
    row = document.createElement('div');
    row.dataset.sa = w.id;
    rows.appendChild(row);
  }
  row.className = `subagent-row subagent-card ${_SA_ROW_CLASS[w.status] || 'is-running'}${_saLive(w) ? ' is-live' : ''}${w.stalled ? ' is-stalled' : ''}`;
  row.dataset.instruction = w.instructionFull || w.instruction || '';
  if (w.sessionId) row.dataset.sessionId = w.sessionId;
  row.innerHTML = _saCardHtml(w, { live, streaming: _parentStreaming() });
  return row;
}

function _saPaintHeader(board, st) {
  const all = st.order.length;
  let done = 0, running = 0, stalled = 0, queued = 0;
  for (const id of st.order) {
    const w = st.workers.get(id);
    if (!w) continue;
    if (!_saLive(w)) done += 1;
    else if (w.status === 'queued') queued += 1;
    else { running += 1; if (w.stalled) stalled += 1; }
  }
  const count = board.querySelector('.subagent-board-count');
  if (count) count.textContent = ` ${done}/${all}`;
  const summary = board.querySelector('.subagent-board-summary');
  if (summary) {
    const bits = [];
    if (running) bits.push(`${running} running`);
    if (queued) bits.push(`${queued} queued`);
    if (stalled) bits.push(`${stalled} stalled`);
    summary.textContent = bits.length ? ` · ${bits.join(' · ')}` : '';
    if (summary.classList) summary.classList.toggle('is-stalled', stalled > 0);
  }
  if (all && done === all) board.dataset.open = '0';
}

function _saTick() {
  const now = Date.now();
  const streaming = _parentStreaming();
  let busy = false;
  try {
    const st = _saState(_currentSessionId, false);
    const cards = document.querySelectorAll('.subagent-card.is-live');
    for (const card of cards) {
      busy = true;
      const w = st && st.workers.get(card.dataset.sa);
      if (!w) continue;
      const el = card.querySelector('.subagent-elapsed');
      if (el) el.textContent = _fmtDur(_saElapsed(w, now));
      const pill = card.querySelector('.subagent-pill');
      if (pill) {
        const p = _saPill(w, now);
        pill.textContent = p.text;
        pill.className = `subagent-pill is-${p.kind}`;
        if (card.classList) card.classList.toggle('is-stalled', p.kind === 'stalled');
      }
      const act = card.querySelector('.subagent-activity');
      if (act) act.textContent = subagentActivity(w);
    }
    // Re-run buttons (live board AND restored history) follow the parent.
    const reruns = document.querySelectorAll('[data-rerun-worker]');
    for (const b of reruns) {
      busy = true;
      b.disabled = streaming;
      b.title = streaming ? 'wait for the delegation to finish' : 'Delegate this task again (optionally with another model)';
    }
  } catch (_) { busy = false; }
  if (!busy && _saTicker) { clearInterval(_saTicker); _saTicker = null; }
}

function _saEnsureTicker() {
  if (_saTicker || typeof setInterval !== 'function') return;
  _saTicker = setInterval(_saTick, 1000);
}

async function _stopWorker(button) {
  const sid = button.dataset.stopWorker;
  if (!sid) return;
  button.disabled = true;
  button.textContent = 'Stopping…';
  const w = _saWorkerByChild(sid);
  if (w) w.stopRequested = true;
  try {
    const r = await fetch(`${API_BASE}/api/chat/subagent/stop/${encodeURIComponent(sid)}`, { method: 'POST', credentials: 'same-origin' });
    const res = r.ok ? await r.json() : { stopped: false };
    button.textContent = res.stopped ? 'Stopped' : 'Not running';
    if (!res.stopped && w) w.stopRequested = false;
  } catch (_) { button.textContent = 'Stop failed'; button.disabled = false; if (w) w.stopRequested = false; }
}

function _saWorkerByChild(childSid) {
  const st = _saState(_currentSessionId, false);
  if (!st) return null;
  for (const id of st.order) {
    const w = st.workers.get(id);
    if (w && w.sessionId === childSid) return w;
  }
  return null;
}

// Inline mini-form inside a worker card (no window.prompt: native dialogs
// block the page, cannot be styled, and are refused by embedded browsers).
// One form per card; a second click on the same button focuses it.
function _inlineForm(card, { kind, placeholder, value = '', submitLabel, onSubmit }) {
  if (!card) return null;
  let form = card.querySelector(`.subagent-inline-form[data-kind="${kind}"]`);
  if (form) {
    const inp = form.querySelector('input,textarea');
    if (inp && inp.focus) inp.focus();
    return form;
  }
  for (const other of card.querySelectorAll('.subagent-inline-form')) other.remove();
  form = document.createElement('form');
  form.className = 'subagent-inline-form';
  form.dataset.kind = kind;
  form.innerHTML =
    `<input type="text" class="subagent-inline-input" placeholder="${esc(placeholder)}" value="${esc(value)}" autocomplete="off">` +
    `<button type="submit" class="harness-btn harness-btn-mini subagent-inline-submit">${esc(submitLabel)}</button>` +
    `<button type="button" class="harness-btn harness-btn-mini subagent-inline-cancel" data-inline-cancel="1">Cancel</button>`;
  const input = form.querySelector('input');
  const close = () => { try { form.remove(); } catch (_) {} };
  form.addEventListener('submit', (e) => {
    if (e && e.preventDefault) e.preventDefault();
    const text = String((input && input.value) || '').trim();
    close();
    onSubmit(text);
  });
  const cancel = form.querySelector('[data-inline-cancel]');
  if (cancel) cancel.addEventListener('click', close);
  if (input) input.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  card.appendChild(form);
  if (input && input.focus) setTimeout(() => input.focus(), 0);
  return form;
}

// "✎ Steer…": inject a message into a running worker (it lands before its
// next round). The line is painted right away; the server's `steer` event
// for the same text is folded into it.
function _steerWorker(button) {
  const sid = button.dataset.steerWorker;
  if (!sid) return;
  const card = button.closest ? button.closest('.subagent-card') : null;
  _inlineForm(card, {
    kind: 'steer',
    placeholder: 'Message for this worker (injected before its next round)',
    submitLabel: 'Send',
    onSubmit: (text) => { if (text) _sendSteer(sid, text, button, card); },
  });
}

async function _sendSteer(sid, text, button, card) {
  if (button) button.disabled = true;
  try {
    const r = await fetch(`${API_BASE}/api/chat/subagent/steer/${encodeURIComponent(sid)}`, {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
    });
    if (r.status === 404) { _toast('This worker is not running any more'); return; }
    if (!r.ok) { _toast('Steer failed'); return; }
    const w = _saWorkerByChild(sid);
    if (w) {
      w.steers.push({ text, source: 'user', at: Date.now(), local: true });
      const board = card && card.closest ? card.closest('.subagent-board') : null;
      if (board) _saPaintWorker(board, w);
    }
    _toast('Steered');
  } catch (_) { _toast('Steer failed'); }
  finally { if (button) button.disabled = false; }
}

// "Re-run" a worker (after a stop / error / partial): re-delegate that single
// task, optionally with another model — through the same /agents path.
function _rerunWorker(button) {
  let task;
  try { task = JSON.parse(button.dataset.rerunWorker || '{}'); } catch (_) { return; }
  if (!task.instruction) return;
  // While the parent turn still streams, a new send would be turned into a
  // Stop of the WHOLE delegation by chat.js (and the payload would stick to
  // the user's next message). Refuse instead of killing the siblings.
  if (_parentStreaming()) {
    _toast('Wait for the delegation to finish before re-running a worker');
    return;
  }
  const card = button.closest ? button.closest('.subagent-card') : null;
  const run = (model) => {
    const sc = window.slashCommandsModule;
    if (sc && typeof sc.delegateTasks === 'function') {
      sc.delegateTasks([{ name: task.name, instruction: task.instruction, files: task.files || [], model }], { parallel: false });
    }
  };
  if (!card) { run(task.model ? String(task.model).trim() : ''); return; }
  // Cancel (Escape / the Cancel button) never re-delegates: another GPU
  // generation, and the worker's files rewritten.
  _inlineForm(card, {
    kind: 'rerun',
    placeholder: 'Model for this worker (empty = same as the chat)',
    value: task.model || '',
    submitLabel: '↻ Run',
    onSubmit: (model) => run(model),
  });
}

/** Worker state from one persisted `tool_events[i].subagents[j]` record. */
function _saFromPersisted(sa, i) {
  const w = _saNewWorker(String(sa.id || sa.session_id || i), 0);
  w.index = sa.index != null ? Number(sa.index) : i;
  _saApply(w, Object.assign({}, sa, { event: 'done' }), 0);
  if (sa.stop_reason == null && !sa.error && sa.status === 'done') w.status = 'done';
  return w;
}

/** Restored history (chatRenderer): the board of a finished delegate_agents
 *  call, rebuilt from the persisted evidence — role, model, tokens, files,
 *  elapsed, steer / supervisor lines, Re-run with the persisted instruction. */
export function restoredSubagentBoardHtml(subagents) {
  const list = Array.isArray(subagents) ? subagents : [];
  if (!list.length) return '';
  const streaming = _parentStreaming();
  const cards = list.map((sa, i) => {
    const w = _saFromPersisted(sa || {}, i);
    return `<div class="subagent-row subagent-card ${_SA_ROW_CLASS[w.status] || 'is-partial'}" data-sa="${esc(w.id)}"${w.sessionId ? ` data-session-id="${esc(w.sessionId)}"` : ''}>${_saCardHtml(w, { live: false, streaming })}</div>`;
  }).join('');
  const doneN = list.filter(sa => sa && !sa.error && sa.stop_reason === 'complete').length;
  _saEnsureTicker();
  return `<div class="subagent-restored subagent-board-v3"><div class="subagent-restored-title">🤖 Sub-agents ${doneN}/${list.length}</div><div class="subagent-rows subagent-cards">${cards}</div></div>`;
}

/** Pure helpers, exposed for the node-based tests. */
export const _subagentInternals = { activity: subagentActivity, apply: _saApply, newWorker: _saNewWorker, elapsed: _saElapsed, pill: _saPill, cardHtml: _saCardHtml, fmtDur: _fmtDur, boards: _saBoards, tick: _saTick, rerun: _rerunWorker, parentStreaming: _parentStreaming };

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

function _rememberTodos(sessionId, todos) {
  if (!sessionId) return;
  _lastTodosBySession.delete(sessionId);           // re-insert => most recent last
  _lastTodosBySession.set(sessionId, todos);
  while (_lastTodosBySession.size > _PROGRESS_CACHE_MAX) {
    const oldest = _lastTodosBySession.keys().next().value;
    if (oldest === undefined) break;
    _lastTodosBySession.delete(oldest);
  }
}

export function renderProgress(todos, { sessionId = null } = {}) {
  const el = _ensureProgressEl();
  const list = el.querySelector('.agent-progress-list');
  const count = el.querySelector('.agent-progress-count');
  if (sessionId) _rememberTodos(sessionId, todos);
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
  _queueCard = null;
  if (!sessionId) { clearProgress(); return; }
  const cached = _lastTodosBySession.get(sessionId);
  // Paint the cache first so the panel does not flash empty on the way back...
  if (cached) renderProgress(cached, { sessionId });
  else { clearProgress(); _renderFiles(sessionId); }
  // ...but ALWAYS re-ask the server. progress_update events are dropped for
  // background chats (chat.js), so the cache is frozen at whatever was on
  // screen when the user left — the "still says 1/5 after it finished" bug.
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


// ── context ledger (FAUSTUS) ────────────────────────────────────────────────
// "The local model ignored my instructions" is usually not a model problem: it
// is 9k of tool schemas, skills and documents spent before the question. This
// card puts the number on screen at the round it happens.

function _fmtTok(n) {
  const v = Number(n) || 0;
  return v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v);
}

export function renderContextLedger(json) {
  const d = json.data || {};
  const sections = Array.isArray(d.sections) ? d.sections : [];
  if (!sections.length) return;
  const head = d.context_length
    ? `Context ${_fmtTok(d.total)} / ${_fmtTok(d.context_length)} · ${d.context_pct}% of the window`
    : `Context ${_fmtTok(d.total)} tokens`;
  const rows = sections.map(s => (
    `<li class="ctx-row"><span class="ctx-row-label">${esc(s.label)}</span>` +
    `<span class="ctx-row-tok">${_fmtTok(s.tokens)}</span>` +
    `<span class="ctx-row-pct">${esc(String(s.pct))}%</span></li>`
  )).join('');
  const advice = (d.advice || []).map(a => (
    `<div class="harness-foot ctx-advice ctx-advice-${esc(a.level || 'info')}">${esc(a.text)}</div>`
  )).join('');
  const slim = d.tool_slim && d.tool_slim.slimmed
    ? `<div class="harness-foot ctx-advice ctx-advice-info">Tool prose trimmed to fit the window: ` +
      `${_fmtTok(d.tool_slim.before)} → ${_fmtTok(d.tool_slim.after)} tokens ` +
      `(descriptions capped at ${esc(String(d.tool_slim.limit))} chars, no tool removed).</div>`
    : '';
  const warn = (d.advice || []).some(a => a.level === 'warn');
  _card(warn ? 'context-warn' : 'context', head,
        `<ul class="harness-list ctx-ledger">${rows}</ul>${slim}${advice}`,
        { open: warn, icon: '🧮' });
}

// ── event wiring ─────────────────────────────────────────────────────────────

export function handleStreamEvent(json, { sessionId = null } = {}) {
  switch (json.type) {
    case 'harness_check': renderHarnessCheck(json); return true;
    case 'harness_summary':
      try { const d = json.data || {}; noteMutations(sessionId || _currentSessionId, d.mutations || [], d.workspace || null); } catch (_) {}
      renderHarnessSummary(json); return true;
    case 'progress_update': renderProgress(json.todos || [], { sessionId }); return true;
    case 'queue_status': renderQueueStatus(json); return true;
    case 'context_ledger': renderContextLedger(json); return true;
    case 'tool_progress':
      if (json.subagent) { renderSubagentEvent(json, { sessionId }); return true; }
      return false;
    default: return false;
  }
}

/** Restored history (chatRenderer): the files row of an old turn with its
 *  restore/commit/review controls, from metrics.harness + the message id. */
export function restoredTurnFilesRow(hz, messageId) {
  const html = turnFilesRowHtml(hz, { messageId });
  if (hz && hz.review_mode && messageId) setTimeout(() => _fetchReviewState(messageId), 0);
  return html;
}

export function init(apiBase) {
  API_BASE = apiBase || '';
  document.addEventListener('odysseus:session-switch', (ev) => {
    const id = ev.detail && ev.detail.id;
    restoreProgress(id || null);
  });
  document.addEventListener('odysseus:message-saved', (ev) => {
    try { _bindMessageId(ev.detail && ev.detail.id); } catch (_) {}
  });
  document.addEventListener('odysseus:review-decided', (ev) => {
    const d = ev.detail || {};
    if (d.messageId && d.state) _applyReviewState(d.messageId, d.state);
  });
  document.addEventListener('click', (e) => {
    const handlers = [
      ['[data-revert-all]', _revertAll],
      ['[data-restore-turn]', _restoreTurn],
      ['[data-commit-turn]', _commitTurn],
      ['[data-review-all]', _reviewAll],
      ['[data-stop-worker]', _stopWorker],
      ['[data-rerun-worker]', _rerunWorker],
      ['[data-steer-worker]', _steerWorker],
    ];
    for (const [sel, fn] of handlers) {
      const b = e.target.closest(sel);
      if (!b || b.disabled) continue;
      e.preventDefault();
      e.stopPropagation();
      fn(b);
      return;
    }
  });
}

const agentHarnessUI = { init, handleStreamEvent, renderHarnessCheck, renderHarnessSummary, renderProgress, restoreProgress, clearProgress, renderSubagentEvent, restoreSubagentBoard, restoredSubagentBoardHtml, subagentBoardState, renderQueueStatus, noteMutations, turnFilesRowHtml, restoredTurnFilesRow };
window.agentHarnessUI = agentHarnessUI;
export default agentHarnessUI;
