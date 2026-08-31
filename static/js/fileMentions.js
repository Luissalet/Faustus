// static/js/fileMentions.js
// "@" file mentions in the chat composer — the picker every other coding
// workspace has (Claude Code's @, Cursor's @, ChatGPT's #).
//
// Typing "@" followed by a few characters opens a ranked list of files in the
// bound workspace; Tab/Enter inserts the workspace-relative path. The server
// (src/file_mentions.py) resolves those paths again when the turn runs and
// hands the model the exact files, so a small local model cannot quietly edit
// a similarly named neighbour instead.
//
// No command logic lives here: ranking is server-side so the popup and the
// prompt agree on what "@ws" means.

const POPUP_ID = 'file-mention-autocomplete';
const MAX_VISIBLE = 12;
const DEBOUNCE_MS = 90;

// The token being typed: "@" then a path run, anchored at the caret. Mirrors
// MENTION_RE in src/file_mentions.py (no whitespace, not glued to a word so
// e-mail addresses never open the menu).
const TRIGGER_RE = /(?:^|[\s(["'`])@([A-Za-z0-9_.][\w./\\-]*)?$/;

function _esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

export function currentWorkspace() {
  try {
    if (window.workspaceModule && window.workspaceModule.getWorkspace) {
      const w = window.workspaceModule.getWorkspace();
      if (w) return w;
    }
  } catch (_) { /* module not loaded yet */ }
  try {
    const raw = localStorage.getItem('odysseus-workspace');
    if (!raw) return '';
    try { const v = JSON.parse(raw); return typeof v === 'string' ? v : (v && v.path) || ''; }
    catch (_) { return raw; }
  } catch (_) { return ''; }
}

// Exported for tests: what is the caret sitting in?
export function activeQuery(value, caret) {
  const before = String(value == null ? '' : value).slice(0, caret);
  const m = TRIGGER_RE.exec(before);
  if (!m) return null;
  const query = m[1] || '';
  // A path with a space needs the quoted form, which the picker writes for you;
  // don't keep the menu open once the user has typed past the token.
  if (query.includes('\n')) return null;
  return { query, start: caret - query.length - 1 };
}

// Exported for tests: the composer value after picking `rel`.
export function applyPick(value, caret, start, rel) {
  const path = /\s/.test(rel) ? `"${rel}"` : rel;
  const head = String(value).slice(0, start);
  const tail = String(value).slice(caret);
  const insert = `@${path}`;
  const sep = tail.startsWith(' ') ? '' : ' ';
  return { value: head + insert + sep + tail, caret: head.length + insert.length + sep.length };
}

function _ensurePopup() {
  let el = document.getElementById(POPUP_ID);
  if (el) return el;
  el = document.createElement('div');
  el.id = POPUP_ID;
  el.className = 'slash-autocomplete-popup file-mention-popup';
  el.setAttribute('role', 'listbox');
  el.setAttribute('aria-label', 'Workspace files');
  document.body.appendChild(el);
  return el;
}

function _position(popup, textarea) {
  const r = textarea.getBoundingClientRect();
  const maxH = Math.min(window.innerHeight * 0.5, 320);
  popup.style.maxHeight = maxH + 'px';
  popup.style.left = Math.round(r.left) + 'px';
  popup.style.width = Math.max(280, Math.round(Math.min(r.width, 520))) + 'px';
  if (r.top > maxH + 20) {
    popup.style.bottom = (window.innerHeight - r.top + 6) + 'px';
    popup.style.top = '';
  } else {
    popup.style.top = (r.bottom + 6) + 'px';
    popup.style.bottom = '';
  }
}

function _render(popup, items, selectedIdx, query, note) {
  if (note) { popup.innerHTML = `<div class="slash-ac-empty">${_esc(note)}</div>`; return; }
  if (!items.length) {
    popup.innerHTML = `<div class="slash-ac-empty">No workspace file matches <code>${_esc(query)}</code></div>`;
    return;
  }
  let html = '<div class="slash-ac-cat">Workspace files</div>';
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    const sel = i === selectedIdx ? ' slash-ac-row-sel' : '';
    const dir = it.dir ? `<span class="slash-ac-help">${_esc(it.dir)}/</span>` : '';
    html += `<div class="slash-ac-row${sel}" role="option" data-idx="${i}" data-rel="${_esc(it.rel)}">`
         +    `<span class="slash-ac-token">${_esc(it.name)}</span>${dir}</div>`;
  }
  popup.innerHTML = html;
  const selEl = popup.querySelector('.slash-ac-row-sel');
  if (selEl) selEl.scrollIntoView({ block: 'nearest' });
}

export function initFileMentions(textarea, opts = {}) {
  if (!textarea || textarea._fileMentionsWired) return;
  textarea._fileMentionsWired = true;

  const fetchFn = opts.fetch || ((url) => fetch(url, { credentials: 'same-origin' }));
  const getWorkspace = opts.getWorkspace || currentWorkspace;
  const cache = new Map();
  let popup = null;
  let visible = false;
  let items = [];
  let selectedIdx = 0;
  let ctx = null;             // { query, start } for the token being completed
  let timer = null;
  let seq = 0;

  const hide = () => {
    if (timer) { clearTimeout(timer); timer = null; }
    if (!visible) return;
    visible = false;
    ctx = null;
    items = [];
    if (popup) popup.style.display = 'none';
  };

  const show = () => {
    if (!popup) popup = _ensurePopup();
    visible = true;
    popup.style.display = 'block';
    _position(popup, textarea);
  };

  const load = async (workspace, query) => {
    const key = query.toLowerCase();
    if (cache.has(key)) return cache.get(key);
    const url = `/api/workspace/files?workspace=${encodeURIComponent(workspace)}`
              + `&q=${encodeURIComponent(query)}&limit=${MAX_VISIBLE}`;
    const res = await fetchFn(url);
    if (!res || !res.ok) throw new Error('lookup failed');
    const data = await res.json();
    const rows = Array.isArray(data && data.files) ? data.files : [];
    if (cache.size > 60) cache.clear();
    cache.set(key, rows);
    return rows;
  };

  const refresh = () => {
    const found = activeQuery(textarea.value, textarea.selectionStart);
    if (!found) { hide(); return; }
    const workspace = getWorkspace();
    if (!workspace) {
      // Nothing to complete against — say so once rather than silently doing
      // nothing, since "@ does nothing" reads as a broken feature.
      ctx = found;
      show();
      _render(popup, [], 0, found.query, 'Bind a workspace folder to mention files with @');
      return;
    }
    ctx = found;
    if (timer) clearTimeout(timer);
    const mySeq = ++seq;
    timer = setTimeout(async () => {
      timer = null;
      let rows = [];
      try {
        rows = await load(workspace, found.query);
      } catch (_) {
        if (mySeq === seq) { hide(); }
        return;
      }
      if (mySeq !== seq) return;                    // a newer keystroke won
      const still = activeQuery(textarea.value, textarea.selectionStart);
      if (!still || still.query !== found.query) return;
      items = rows.slice(0, MAX_VISIBLE);
      selectedIdx = 0;
      if (!items.length && found.query.length > 2) { hide(); return; }
      show();
      _render(popup, items, selectedIdx, found.query, '');
    }, DEBOUNCE_MS);
  };

  const insert = (rel) => {
    if (!ctx) return;
    const next = applyPick(textarea.value, textarea.selectionStart, ctx.start, rel);
    textarea.value = next.value;
    hide();
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    textarea.focus();
    try { textarea.setSelectionRange(next.caret, next.caret); } catch (_) {}
  };

  textarea.addEventListener('input', refresh);
  textarea.addEventListener('click', refresh);
  textarea.addEventListener('focus', refresh);
  textarea.addEventListener('blur', () => { setTimeout(hide, 120); });
  // The composer is wired from a dynamic import, so text can already be in it
  // by the time we get here — someone typing into a cold page, or a restored
  // draft. Without this first pass the picker stays shut until the next
  // keystroke, which reads as "@ does nothing".
  refresh();

  textarea.addEventListener('keydown', (e) => {
    if (!visible) return;
    if (e.key === 'Escape') { e.preventDefault(); hide(); return; }
    if (!items.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectedIdx = (selectedIdx + 1) % items.length;
      _render(popup, items, selectedIdx, ctx ? ctx.query : '', '');
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectedIdx = (selectedIdx - 1 + items.length) % items.length;
      _render(popup, items, selectedIdx, ctx ? ctx.query : '', '');
    } else if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
      e.preventDefault();
      e.stopPropagation();
      insert(items[selectedIdx].rel);
    }
  }, true);   // capture: beat the composer's own Enter-to-send handler

  window.addEventListener('resize', () => { if (visible && popup) _position(popup, textarea); });

  document.addEventListener('mousedown', (e) => {
    if (!visible || !popup) return;
    const row = e.target.closest ? e.target.closest('.slash-ac-row') : null;
    if (row && popup.contains(row) && row.dataset.rel) {
      e.preventDefault();
      insert(row.dataset.rel);
    }
  });
}

export default { initFileMentions, activeQuery, applyPick, currentWorkspace };
