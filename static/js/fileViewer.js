// fileViewer.js — right-hand review panel for files the agent touched.
//
// Opened from the "Edited N files" chips in the harness cards (agentHarnessUI)
// and from any element with data-open-file="<path>" [data-open-workspace].
// Shows the file with line numbers, a git diff of the working tree for it,
// copy / open-in-folder / raw actions. Read-only: reviewing, not editing.
//
// Backend: GET /api/workspace/file, GET /api/workspace/file_diff,
//          POST /api/workspace/reveal (routes/workspace_routes.py).

let API_BASE = '';
let _panel = null;
let _state = { path: null, workspace: null, mode: 'file', data: null, diff: null, checkpoint: null, reviewMsg: null };

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function _workspaceFallback() {
  // The bound workspace lives in localStorage ('odysseus-workspace', see
  // static/js/workspace.js); older persisted answers carry no workspace.
  try {
    if (window.workspaceModule && window.workspaceModule.getWorkspace) {
      const w = window.workspaceModule.getWorkspace();
      if (w) return w;
    }
  } catch (_) {}
  try {
    const raw = localStorage.getItem('odysseus-workspace');
    if (!raw) return '';
    try { const v = JSON.parse(raw); return typeof v === 'string' ? v : (v && v.path) || ''; } catch (_) { return raw; }
  } catch (_) { return ''; }
}

function _ensurePanel() {
  if (_panel && document.body.contains(_panel)) return _panel;
  const el = document.createElement('aside');
  el.id = 'file-viewer-panel';
  el.className = 'file-viewer-panel';
  el.hidden = true;
  el.innerHTML =
    `<div class="fv-head">` +
    `<div class="fv-title"><span class="fv-kicker">View file</span><span class="fv-crumbs"></span></div>` +
    `<div class="fv-nav" hidden><button type="button" class="fv-btn fv-icon" data-fv="prev" title="Previous file (←)">‹</button><span class="fv-nav-pos"></span><button type="button" class="fv-btn fv-icon" data-fv="next" title="Next file (→)">›</button></div>` +
    `<div class="fv-actions">` +
    `<button type="button" class="fv-btn" data-fv="file" title="File contents">File</button>` +
    `<button type="button" class="fv-btn" data-fv="diff" title="Changes vs. git HEAD">Diff</button>` +
    `<button type="button" class="fv-btn fv-icon" data-fv="copy" title="Copy contents"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>` +
    `<button type="button" class="fv-btn fv-icon" data-fv="reveal" title="Show in folder"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></button>` +
    `<button type="button" class="fv-btn fv-icon" data-fv="editor" title="Open in editor (VS Code)"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg></button>` +
    `<span class="fv-review" hidden><button type="button" class="fv-btn fv-accept" data-fv="accept" title="Keep this change (review mode)">✓ Accept</button><button type="button" class="fv-btn fv-reject" data-fv="reject" title="Discard this change — the file goes back to its state before the turn">✗ Reject</button></span>` +
    `<button type="button" class="fv-btn fv-icon fv-danger" data-fv="revert" title="Revert this file's changes (checkpoint of the turn, or git checkout)"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg></button>` +
    `<button type="button" class="fv-btn fv-icon" data-fv="raw" title="Open raw in a new tab"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg></button>` +
    `<button type="button" class="fv-btn fv-icon fv-close" data-fv="close" title="Close">×</button>` +
    `</div></div>` +
    `<div class="fv-meta"></div>` +
    `<div class="fv-body"><pre class="fv-code"></pre></div>`;
  document.body.appendChild(el);
  el.addEventListener('click', (e) => {
    const b = e.target.closest('[data-fv]');
    if (!b) return;
    const a = b.dataset.fv;
    if (a === 'close') close();
    else if (a === 'file') { _state.mode = 'file'; _render(); }
    else if (a === 'diff') { _state.mode = 'diff'; _loadDiff().then(_render); }
    else if (a === 'copy') _copy();
    else if (a === 'reveal') _reveal();
    else if (a === 'editor') _openEditor();
    else if (a === 'revert') _revert();
    else if (a === 'accept') _decide('accept');
    else if (a === 'reject') _decide('reject');
    else if (a === 'raw') _openRaw();
    else if (a === 'prev') _nav(-1);
    else if (a === 'next') _nav(1);
  });
  document.addEventListener('keydown', (e) => {
    if (el.hidden) return;
    if (e.key === 'Escape') { close(); return; }
    // ← / → step through the files of the group the viewer was opened from,
    // unless the user is typing somewhere.
    const t = e.target;
    const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    if (typing || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); _nav(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); _nav(1); }
  });
  _panel = el;
  return el;
}

function _crumbs(path) {
  const parts = String(path || '').split(/[\\/]+/).filter(Boolean);
  return parts.map((p, i) => `<span class="fv-crumb${i === parts.length - 1 ? ' is-last' : ''}">${esc(p)}</span>`).join('<span class="fv-sep">›</span>');
}

function _highlight(code, path) {
  try {
    if (window.hljs) {
      const ext = (String(path).split('.').pop() || '').toLowerCase();
      const lang = { js: 'javascript', mjs: 'javascript', cjs: 'javascript', ts: 'typescript', py: 'python', json: 'json', css: 'css', html: 'xml', htm: 'xml', md: 'markdown', sh: 'bash', ps1: 'powershell', yml: 'yaml', yaml: 'yaml', toml: 'ini', sql: 'sql', diff: 'diff' }[ext];
      if (lang && window.hljs.getLanguage && window.hljs.getLanguage(lang)) return window.hljs.highlight(code, { language: lang }).value;
      return window.hljs.highlightAuto ? window.hljs.highlightAuto(code).value : esc(code);
    }
  } catch (_) {}
  return esc(code);
}

function _render() {
  const el = _ensurePanel();
  const d = _state.data;
  el.querySelector('.fv-crumbs').innerHTML = _crumbs(_state.path);
  el.querySelectorAll('.fv-btn[data-fv="file"], .fv-btn[data-fv="diff"]').forEach(b => b.classList.toggle('active', b.dataset.fv === _state.mode));
  const nav = el.querySelector('.fv-nav');
  const many = Array.isArray(_state.list) && _state.list.length > 1;
  nav.hidden = !many;
  if (many) nav.querySelector('.fv-nav-pos').textContent = `${_state.index + 1}/${_state.list.length}`;
  const meta = el.querySelector('.fv-meta');
  const code = el.querySelector('.fv-code');
  if (!d) { meta.textContent = 'Loading…'; code.innerHTML = ''; el.hidden = false; return; }
  if (d.error) { meta.textContent = d.error; code.innerHTML = ''; el.hidden = false; return; }
  if (_state.mode === 'diff') {
    const df = _state.diff;
    if (!df) { meta.textContent = 'Loading diff…'; code.innerHTML = ''; }
    else if (!df.git) { meta.textContent = 'Not a git repository and no checkpoint for this turn — no diff available.'; code.innerHTML = ''; }
    else if (!df.diff) { meta.textContent = df.checkpoint ? 'No changes vs. the checkpoint taken before this turn.' : 'No changes vs. HEAD for this file.'; code.innerHTML = ''; }
    else {
      const lines = df.diff.split('\n');
      meta.textContent = `${df.checkpoint ? 'diff vs. before this turn' : 'git diff'} · ${lines.filter(l => l.startsWith('+') && !l.startsWith('+++')).length} added, ${lines.filter(l => l.startsWith('-') && !l.startsWith('---')).length} removed`;
      code.innerHTML = lines.map(l => {
        const cls = l.startsWith('+') && !l.startsWith('+++') ? 'fv-add' : (l.startsWith('-') && !l.startsWith('---') ? 'fv-del' : (l.startsWith('@@') ? 'fv-hunk' : ''));
        return `<span class="fv-line ${cls}">${esc(l) || '&nbsp;'}</span>`;
      }).join('');
    }
  } else {
    const ext = (String(_state.path).split('.').pop() || '').toUpperCase();
    meta.textContent = d.binary ? 'Binary file' : `${ext} · ${d.lines} lines · ${(d.size / 1024).toFixed(1)} KB${d.truncated ? ' · truncated' : ''}`;
    if (d.binary) code.innerHTML = '<span class="fv-line">(binary file — nothing to show)</span>';
    else {
      const html = _highlight(d.text, _state.path);
      const parts = html.split('\n');
      code.innerHTML = parts.map((l, i) => `<span class="fv-line"><span class="fv-ln">${i + 1}</span>${l || '&nbsp;'}</span>`).join('');
    }
  }
  const rv = el.querySelector('.fv-review');
  if (rv) {
    rv.hidden = !_state.reviewMsg;
    const st = _state.reviewState;
    rv.querySelectorAll('.fv-btn').forEach(b => b.classList.remove('active'));
    if (st === 'accepted') { const b = rv.querySelector('[data-fv="accept"]'); if (b) b.classList.add('active'); }
    if (st === 'rejected') { const b = rv.querySelector('[data-fv="reject"]'); if (b) b.classList.add('active'); }
  }
  el.hidden = false;
  document.body.classList.add('file-viewer-open');
}

async function _load() {
  _state.data = null; _state.diff = null;
  _render();
  try {
    const q = `workspace=${encodeURIComponent(_state.workspace || '')}&path=${encodeURIComponent(_state.path || '')}`;
    const r = await fetch(`${API_BASE}/api/workspace/file?${q}`, { credentials: 'same-origin' });
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try { msg = (await r.json()).detail || msg; } catch (_) {}
      _state.data = { error: `Could not open the file: ${msg}` };
    } else {
      _state.data = await r.json();
    }
  } catch (e) { _state.data = { error: String(e) }; }
  _render();
}

async function _loadDiff() {
  if (_state.diff) return;
  try {
    const cp = _state.checkpoint ? `&checkpoint=${encodeURIComponent(_state.checkpoint)}` : '';
    const q = `workspace=${encodeURIComponent(_state.workspace || '')}&path=${encodeURIComponent(_state.path || '')}${cp}`;
    const r = await fetch(`${API_BASE}/api/workspace/file_diff?${q}`, { credentials: 'same-origin' });
    _state.diff = r.ok ? await r.json() : { git: false, diff: '' };
  } catch (_) { _state.diff = { git: false, diff: '' }; }
}

async function _copy() {
  try {
    const text = _state.mode === 'diff' ? ((_state.diff && _state.diff.diff) || '') : ((_state.data && _state.data.text) || '');
    await navigator.clipboard.writeText(text);
    const b = _panel.querySelector('[data-fv="copy"]'); if (b) { b.classList.add('done'); setTimeout(() => b.classList.remove('done'), 900); }
  } catch (_) {}
}

async function _reveal() {
  try {
    const q = `workspace=${encodeURIComponent(_state.workspace || '')}&path=${encodeURIComponent(_state.path || '')}`;
    await fetch(`${API_BASE}/api/workspace/reveal?${q}`, { method: 'POST', credentials: 'same-origin' });
  } catch (_) {}
}

async function _openEditor() {
  try {
    const q = `workspace=${encodeURIComponent(_state.workspace || '')}&path=${encodeURIComponent(_state.path || '')}`;
    const r = await fetch(`${API_BASE}/api/workspace/open_editor?${q}`, { method: 'POST', credentials: 'same-origin' });
    const meta = _panel && _panel.querySelector('.fv-meta');
    if (meta) meta.textContent = r.ok ? `Opened in ${(await r.json()).editor || 'editor'}` : `Could not open the editor (HTTP ${r.status})`;
  } catch (_) {}
}

async function _revert() {
  const name = String(_state.path || '').split(/[\\/]/).pop();
  const how = _state.checkpoint ? 'back to its state before this turn' : 'git checkout — an untracked new file is deleted';
  let ok = false;
  try {
    ok = window.uiModule && window.uiModule.styledConfirm
      ? await window.uiModule.styledConfirm(`Revert all changes to ${name}? (${how})`, { confirmText: 'Revert', danger: true })
      : window.confirm(`Revert all changes to ${name}?`);
  } catch (_) { ok = window.confirm(`Revert all changes to ${name}?`); }
  if (!ok) return;
  try {
    const cp = _state.checkpoint ? `&checkpoint=${encodeURIComponent(_state.checkpoint)}` : '';
    const q = `workspace=${encodeURIComponent(_state.workspace || '')}&path=${encodeURIComponent(_state.path || '')}${cp}`;
    const r = await fetch(`${API_BASE}/api/workspace/revert?${q}`, { method: 'POST', credentials: 'same-origin' });
    const meta = _panel && _panel.querySelector('.fv-meta');
    if (!r.ok) {
      let msg = `HTTP ${r.status}`; try { msg = (await r.json()).detail || msg; } catch (_) {}
      if (meta) meta.textContent = `Revert failed: ${msg}`;
      return;
    }
    const res = await r.json();
    if (res.action === 'deleted_untracked' || res.action === 'deleted_new_file') { _state.data = { error: `${name} was a new file and has been deleted.` }; _state.diff = null; _render(); return; }
    _state.diff = null;
    await _load();
    if (meta) meta.textContent = (res.action === 'restored' ? (res.checkpoint ? 'Restored to before this turn · ' : 'Restored from git HEAD · ') : 'No changes to revert · ') + meta.textContent;
  } catch (_) {}
}

/** Review mode: accept keeps the change, reject restores the file from the
 *  turn's checkpoint. Bookkeeping lives server-side (services/review_state). */
async function _decide(decision) {
  if (!_state.reviewMsg || !_state.path) return;
  const name = String(_state.path || '').split(/[\\/]/).pop();
  if (decision === 'reject') {
    let ok = false;
    try {
      ok = window.uiModule && window.uiModule.styledConfirm
        ? await window.uiModule.styledConfirm(`Reject the change to ${name}? The file goes back to its state before the turn.`, { confirmText: 'Reject', danger: true })
        : window.confirm(`Reject the change to ${name}?`);
    } catch (_) { ok = window.confirm(`Reject the change to ${name}?`); }
    if (!ok) return;
  }
  const meta = _panel && _panel.querySelector('.fv-meta');
  try {
    const r = await fetch(`${API_BASE}/api/workspace/review/${encodeURIComponent(_state.reviewMsg)}/decide`, {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: _state.path, decision }),
    });
    if (!r.ok) {
      let msg = `HTTP ${r.status}`; try { msg = (await r.json()).detail || msg; } catch (_) {}
      if (meta) meta.textContent = `Could not record the decision: ${msg}`;
      return;
    }
    const res = await r.json();
    _state.reviewState = decision === 'accept' ? 'accepted' : 'rejected';
    try { document.dispatchEvent(new CustomEvent('odysseus:review-decided', { detail: { messageId: _state.reviewMsg, path: _state.path, decision, state: res.state } })); } catch (_) {}
    if (decision === 'reject') {
      if (res.action === 'deleted_new_file') { _state.data = { error: `${name} was a new file and has been deleted (rejected).` }; _state.diff = null; _render(); return; }
      _state.diff = null;
      await _load();
      if (meta) meta.textContent = 'Rejected — restored to before this turn · ' + meta.textContent;
    } else {
      _render();
      if (meta) meta.textContent = 'Accepted · ' + meta.textContent;
    }
  } catch (_) {}
}

function _openRaw() {
  const text = (_state.data && _state.data.text) || '';
  try {
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    window.open(url, '_blank');
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (_) {}
}

/** Open a file. `list` (optional) is the group of files the viewer can step
 *  through with ‹ › / arrow keys: [{path, workspace}], `index` = position. */
export function open(path, { workspace = null, mode = 'file', list = null, index = 0, checkpoint = null, reviewMsg = null, reviewState = null } = {}) {
  if (!path) return;
  const group = Array.isArray(list) && list.length > 1 ? list : null;
  _state = { path, workspace: workspace || _workspaceFallback(), mode, data: null, diff: null, list: group,
             index: group ? Math.max(0, Math.min(index, group.length - 1)) : 0,
             checkpoint: checkpoint || null, reviewMsg: reviewMsg || null, reviewState: reviewState || null };
  _ensurePanel();
  _load().then(() => { if (mode === 'diff') _loadDiff().then(_render); });
}

function _nav(delta) {
  const l = _state.list;
  if (!l || l.length < 2) return;
  const i = (_state.index + delta + l.length) % l.length;
  const it = l[i];
  open(it.path, { workspace: it.workspace || _state.workspace, mode: _state.mode, list: l, index: i,
                  checkpoint: it.checkpoint || _state.checkpoint, reviewMsg: it.reviewMsg || _state.reviewMsg, reviewState: it.reviewState || null });
}

/** Files edited by the agent in the current chat, as a navigable group. */
function _groupFrom(anchor) {
  const group = anchor.closest('.harness-files') || anchor.parentElement;
  const chips = group ? [...group.querySelectorAll('[data-open-file]')] : [anchor];
  const seen = new Set();
  const list = [];
  let index = 0;
  for (const c of chips) {
    const p = c.dataset.openFile;
    if (!p || seen.has(p)) continue;
    seen.add(p);
    if (c === anchor) index = list.length;
    list.push({ path: p, workspace: c.dataset.openWorkspace || null, checkpoint: c.dataset.openCheckpoint || null,
                reviewMsg: c.dataset.reviewMsg || null, reviewState: c.dataset.reviewState || null });
  }
  return { list, index };
}

export function close() {
  if (_panel) _panel.hidden = true;
  document.body.classList.remove('file-viewer-open');
}

export function isOpen() { return !!(_panel && !_panel.hidden); }

export function init(apiBase) {
  API_BASE = apiBase || '';
  // Any element with data-open-file opens the viewer (harness cards, tool cards…).
  document.addEventListener('click', (e) => {
    const a = e.target.closest('[data-open-file]');
    if (!a) return;
    e.preventDefault();
    e.stopPropagation();
    const { list, index } = _groupFrom(a);
    open(a.dataset.openFile, { workspace: a.dataset.openWorkspace || null, mode: a.dataset.openMode || 'file', list, index,
                               checkpoint: a.dataset.openCheckpoint || null, reviewMsg: a.dataset.reviewMsg || null, reviewState: a.dataset.reviewState || null });
  });
}

const fileViewer = { init, open, close, isOpen };
window.fileViewer = fileViewer;
export default fileViewer;
