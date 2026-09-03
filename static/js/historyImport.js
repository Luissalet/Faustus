// static/js/historyImport.js
// Imported history — your conversations from somewhere else, brought here.
//
// The backend is authoritative (src/history_import.py,
// routes/history_import_routes.py — NOT routes/history_routes.py, which is the
// chat history's own module):
// the parsers, the normalised store in DATA_DIR/history.db and the two-tier
// search all live there. This module is the human end of it — an import form
// that shows the DRY RUN first, the list of what came in, a reader for one
// conversation, and a search box over everything.
//
// Three rules run through the whole file and are not negotiable:
//
//   * the import ALWAYS previews first. The dry run's counts and its skipped
//     list are shown, and the real run is a second, deliberate click;
//   * a skipped conversation is rendered WITH its reason. "6 of 900 skipped"
//     with no reason is a bug report the user cannot file; and
//   * a result list always prints its tier. `degraded: true` means the refined
//     lane was missing, and the page says so instead of quietly serving worse
//     results as if they were the best available.
//
// The renderers are pure and live between the marked region below, so
// tests/test_history_import_page_js.py can run them in bare node.

import uiModule from './ui.js';

const API = `${window.location.origin}/api/history`;

// ── History: pure helpers (dependency-free; extracted and run under node by tests) ──
// Everything between these markers must stay free of DOM, module and window
// references so tests/test_history_import_page_js.py can execute it in bare node.

/** Local escape: same table as ui.js esc(), but import-free for tests. */
function hisEsc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

/** Sanitize a server-provided word (a source name) for use in a class. */
function hisToken(value) {
  return String(value == null ? '' : value).toLowerCase().replace(/[^a-z0-9_-]/g, '');
}

/** A number, or `fallback` when the value is not one. Never NaN. */
function hisNum(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

/** An integer, or null when the value cannot be one (never a guess). */
function hisInt(value) {
  if (value == null || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

const HIS_SOURCES = ['chatgpt', 'claude', 'lmstudio', 'faustus'];

const HIS_SOURCE_LABELS = {
  chatgpt: 'ChatGPT',
  claude: 'Claude',
  lmstudio: 'LM Studio',
  faustus: 'Faustus',
};

/** The display name of a source, or the raw slug when it is not one we know. */
function sourceLabel(value) {
  const key = String(value == null ? '' : value).trim().toLowerCase();
  if (!key) return 'unknown';
  return HIS_SOURCE_LABELS[key] || key;
}

/**
 * "3 Jan 2026" — or "date unknown" when the export did not record one.
 *
 * The store keeps an unreadable timestamp as null rather than stamping it with
 * the import time, and this is where that honesty has to survive: a
 * conversation with no date NEVER gets today's.
 */
function dateLabel(value) {
  const raw = String(value == null ? '' : value).trim();
  // Only an ISO-8601 date is a date. `new Date("0")` happily answers the year
  // 2000, and printing that for a field the store deliberately left null is
  // exactly the fabrication this label exists to prevent.
  if (!/^\d{4}-\d{2}-\d{2}/.test(raw)) return 'date unknown';
  const when = new Date(raw);
  if (Number.isNaN(when.getTime())) return 'date unknown';
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return `${when.getUTCDate()} ${months[when.getUTCMonth()]} ${when.getUTCFullYear()}`;
}

/** One conversation row from GET /conversations, every field defaulted. */
function normalizeConversation(raw) {
  const row = raw && typeof raw === 'object' ? raw : {};
  return {
    id: String(row.id == null ? '' : row.id),
    source: String(row.source || ''),
    external_id: String(row.external_id || ''),
    title: String(row.title || ''),
    // null stays null: "we do not know when this was said".
    started_at: row.started_at || null,
    ended_at: row.ended_at || null,
    model: String(row.model || ''),
    message_count: hisNum(row.message_count, 0),
    imported_at: row.imported_at || '',
    path: String(row.path || ''),
  };
}

/** GET /conversations: a wrapper, a bare list, or a robot-mode {"data": …}. */
function normalizeConversations(raw) {
  let list = [];
  if (Array.isArray(raw)) list = raw;
  else if (raw && typeof raw === 'object') {
    if (Array.isArray(raw.conversations)) list = raw.conversations;
    else if (Array.isArray(raw.items)) list = raw.items;
    else if (raw.data && typeof raw.data === 'object' && Array.isArray(raw.data.conversations)) {
      list = raw.data.conversations;
    }
  }
  return list
    .filter(row => row && typeof row === 'object' && row.id != null && String(row.id))
    .map(normalizeConversation);
}

/** GET /stats — the counters the header prints. */
function normalizeStats(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const stats = data.stats && typeof data.stats === 'object' ? data.stats : data;
  const sources = Array.isArray(stats.sources) ? stats.sources : [];
  return {
    conversations: hisNum(stats.conversations, 0),
    messages: hisNum(stats.messages, 0),
    oldest: stats.oldest || null,
    newest: stats.newest || null,
    enabled: stats.enabled === undefined ? true : Boolean(stats.enabled),
    sources: sources
      .filter(row => row && typeof row === 'object' && row.source)
      .map(row => ({
        source: String(row.source),
        conversations: hisNum(row.conversations, 0),
        messages: hisNum(row.messages, 0),
      })),
  };
}

/** POST /import — the dry run's report and the real run's, same shape. */
function normalizeImport(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const skipped = Array.isArray(data.skipped) ? data.skipped : [];
  return {
    detected: String(data.detected || ''),
    files: hisNum(data.files, 0),
    conversations: hisNum(data.conversations, 0),
    messages: hisNum(data.messages, 0),
    created: hisNum(data.created, 0),
    updated: hisNum(data.updated, 0),
    seconds: hisNum(data.seconds, 0),
    dry_run: Boolean(data.dry_run),
    skipped: skipped
      .filter(row => row && typeof row === 'object')
      .map(row => ({ why: String(row.why || 'no reason given'), where: String(row.where || '') })),
  };
}

/** GET /conversations/{id} — one conversation and its messages, in order. */
function normalizeDetail(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const conv = data.conversation && typeof data.conversation === 'object'
    ? data.conversation : data;
  const messages = Array.isArray(conv.messages) ? conv.messages : [];
  return {
    ...normalizeConversation(conv),
    messages: messages
      .filter(row => row && typeof row === 'object')
      .map((row, index) => ({
        id: String(row.id == null ? `m${index}` : row.id),
        role: String(row.role || 'user'),
        content: String(row.content == null ? '' : row.content),
        ts: row.ts || null,
        ordinal: hisNum(row.ordinal, index),
      })),
  };
}

/** GET /search — hits plus the tier that answered. `degraded` is not an error. */
function normalizeSearch(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const hits = Array.isArray(data.hits) ? data.hits : [];
  return {
    query: String(data.query || ''),
    tier: String(data.tier || 'lexical'),
    degraded: Boolean(data.degraded),
    candidates: hisNum(data.candidates, 0),
    elapsed_ms: hisNum(data.elapsed_ms, 0),
    hits: hits.filter(hit => hit && typeof hit === 'object').map(hit => ({
      message_id: String(hit.message_id || ''),
      conversation_id: String(hit.conversation_id || ''),
      title: String(hit.title || ''),
      source: String(hit.source || ''),
      role: String(hit.role || ''),
      ts: hit.ts || null,
      score: hisNum(hit.score, 0),
      snippet: String(hit.snippet == null ? '' : hit.snippet),
      snippet_start: hisNum(hit.snippet_start, 0),
      // null when nothing literally matched — a highlight is never invented.
      match_start: hisInt(hit.match_start),
      match_end: hisInt(hit.match_end),
    })),
  };
}

/** Filter by source; a blank or unknown value keeps everything. */
function filterBySource(rows, source) {
  const key = String(source == null ? '' : source).trim().toLowerCase();
  if (!key || key === 'all') return Array.isArray(rows) ? rows.slice() : [];
  return (Array.isArray(rows) ? rows : []).filter(row => String(row.source || '') === key);
}

/**
 * The snippet with the matched span wrapped in a <mark>, escaped either way.
 *
 * `match_start`/`match_end` are offsets into the FULL message, and
 * `snippet_start` is where the excerpt begins in it, so the highlight is the
 * span the backend actually found — not a re-search of the excerpt, which
 * would happily mark a second, different occurrence. When there was no match
 * the text is printed plain; nothing is highlighted that was not matched.
 */
function highlightSnippet(hit) {
  const row = hit && typeof hit === 'object' ? hit : {};
  const text = String(row.snippet == null ? '' : row.snippet);
  const start = hisInt(row.match_start);
  const end = hisInt(row.match_end);
  const base = hisNum(row.snippet_start, 0);
  if (start == null || end == null || end <= start) return hisEsc(text);
  const from = start - base;
  const to = end - base;
  if (from < 0 || to > text.length || from >= to) return hisEsc(text);
  return `${hisEsc(text.slice(0, from))}<mark class="his-mark">${hisEsc(text.slice(from, to))}</mark>${hisEsc(text.slice(to))}`;
}

/** "lexical" / "hybrid" / "refined" as a chip the user can act on. */
function tierChipHtml(tier, degraded) {
  const key = hisToken(tier) || 'lexical';
  const note = degraded
    ? 'no embedding model available — BM25 and hash vectors only'
    : 'the full embedder answered';
  return `<span class="his-tier is-${key}" title="${hisEsc(note)}">tier: ${hisEsc(key)}</span>`
    + (degraded ? '<span class="his-degraded">degraded</span>' : '');
}

/** The import form. Nothing is imported until the dry run has been shown. */
function importFormHtml(state) {
  const s = state && typeof state === 'object' ? state : {};
  const busy = Boolean(s.busy);
  const path = hisEsc(s.path == null ? '' : s.path);
  const source = hisToken(s.source);
  const options = ['<option value="">detect automatically</option>'].concat(
    HIS_SOURCES.map(key => `<option value="${key}"${source === key ? ' selected' : ''}>`
      + `${hisEsc(HIS_SOURCE_LABELS[key])}</option>`),
  ).join('');
  return `<form class="his-import" data-his-import-form>
    <h4>Import an export</h4>
    <p class="his-hint">A ChatGPT or Claude <code>conversations.json</code>, a folder of LM Studio
      chats, or one of this app's own JSON exports. Nothing is written until you have seen the
      preview.</p>
    <label class="his-field"><span>Path on this machine</span>
      <input type="text" data-his-path value="${path}"
             placeholder="/home/you/Downloads/chatgpt-export"${busy ? ' disabled' : ''}></label>
    <label class="his-field"><span>…or upload a file</span>
      <input type="file" accept=".json,application/json" data-his-file${busy ? ' disabled' : ''}></label>
    <label class="his-field"><span>Format</span>
      <select data-his-source${busy ? ' disabled' : ''}>${options}</select></label>
    <div class="his-import-actions">
      <button type="submit" class="his-btn is-primary"${busy ? ' disabled' : ''}>
        ${busy ? 'Reading…' : 'Preview import'}</button>
    </div>
    <div class="his-error" data-his-import-error${s.error ? '' : ' hidden'}>${hisEsc(s.error || '')}</div>
  </form>`;
}

/**
 * The dry run's report, and the button that commits it.
 *
 * Every skipped conversation is listed with its reason. A summary that says
 * "6 skipped" and nothing else is a bug report the user cannot file.
 */
function importPreviewHtml(report) {
  if (!report || typeof report !== 'object') return '';
  const data = normalizeImport(report);
  const detected = data.detected
    ? `<b>${hisEsc(sourceLabel(data.detected))}</b>`
    : '<b>nothing recognised</b>';
  const skipped = data.skipped.length
    ? `<details class="his-skipped" open><summary>${data.skipped.length} skipped</summary><ul>`
      + data.skipped.map(row => `<li><span class="his-skip-where">${hisEsc(row.where)}</span>`
        + `<span class="his-skip-why">${hisEsc(row.why)}</span></li>`).join('')
      + '</ul></details>'
    : '<p class="his-hint">Nothing was skipped.</p>';
  const verb = data.dry_run ? 'would import' : 'imported';
  const commit = data.dry_run && data.conversations > 0
    ? `<div class="his-import-actions">
         <button type="button" class="his-btn is-primary" data-his-commit>Import them</button>
         <button type="button" class="his-btn" data-his-cancel>Cancel</button>
       </div>`
    : '';
  return `<div class="his-preview" data-his-preview>
    <h4>${data.dry_run ? 'Preview' : 'Imported'}</h4>
    <p class="his-counts">Detected ${detected} in <b>${data.files}</b> file(s): ${verb}
      <b>${data.conversations}</b> conversations and <b>${data.messages}</b> messages
      — <b>${data.created}</b> new, <b>${data.updated}</b> already here
      <span class="his-secs">(${data.seconds.toFixed(2)}s)</span></p>
    ${skipped}
    ${commit}
  </div>`;
}

/** The counters above the list: how much past is in here, and over what span. */
function statsHtml(stats) {
  const data = normalizeStats(stats);
  const per = data.sources.length
    ? data.sources.map(row => `<span class="his-stat-chip is-${hisToken(row.source)}">`
      + `${hisEsc(sourceLabel(row.source))} <b>${row.conversations}</b></span>`).join('')
    : '';
  const span = (data.oldest || data.newest)
    ? `<span class="his-span">${hisEsc(dateLabel(data.oldest))} – ${hisEsc(dateLabel(data.newest))}</span>`
    : '';
  return `<div class="his-stats">
    <span class="his-stat"><b>${data.conversations}</b> conversations</span>
    <span class="his-stat"><b>${data.messages}</b> messages</span>
    ${span}${per}
  </div>`;
}

/** One row of the conversation list. */
function conversationRowHtml(row) {
  const conv = normalizeConversation(row);
  const model = conv.model ? `<span class="his-row-model">${hisEsc(conv.model)}</span>` : '';
  return `<li class="his-row">
    <button type="button" class="his-row-open" data-his-open="${hisEsc(conv.id)}">
      <span class="his-row-title">${hisEsc(conv.title || 'Untitled')}</span>
      <span class="his-row-meta">
        <span class="his-src is-${hisToken(conv.source)}">${hisEsc(sourceLabel(conv.source))}</span>
        <span class="his-row-date">${hisEsc(dateLabel(conv.started_at))}</span>
        <span class="his-row-count">${conv.message_count} messages</span>
        ${model}
      </span>
    </button>
    <button type="button" class="his-btn is-danger" data-his-delete="${hisEsc(conv.id)}"
            title="Delete this imported conversation">Delete</button>
  </li>`;
}

/** The whole list view: filter, states, rows. */
function conversationListHtml(rows, state) {
  const s = state && typeof state === 'object' ? state : {};
  const list = normalizeConversations(rows);
  const visible = filterBySource(list, s.source);
  const off = s.enabled === false
    ? '<div class="his-off">Imported history is off in Settings → Agent tools '
      + '(<code>agent_history_import</code>). Everything already imported is still on disk.</div>'
    : '';
  const filter = ['<option value="">every source</option>'].concat(
    HIS_SOURCES.map(key => `<option value="${key}"${hisToken(s.source) === key ? ' selected' : ''}>`
      + `${hisEsc(HIS_SOURCE_LABELS[key])}</option>`),
  ).join('');
  let body;
  if (s.loading) body = '<div class="his-empty">Loading imported history…</div>';
  else if (!list.length) body = '<div class="his-empty">Nothing imported yet.</div>';
  else if (!visible.length) body = '<div class="his-empty">No conversation from that source.</div>';
  else body = `<ul class="his-list">${visible.map(conversationRowHtml).join('')}</ul>`;
  return `${off}${statsHtml(s.stats)}
    <div class="his-toolbar">
      <label class="his-field is-inline"><span>Source</span>
        <select data-his-filter>${filter}</select></label>
      <form class="his-search" data-his-search-form>
        <input type="search" data-his-query value="${hisEsc(s.query || '')}"
               placeholder="Search every imported message">
        <button type="submit" class="his-btn">Search</button>
      </form>
    </div>
    <div class="his-error" data-his-error${s.error ? '' : ' hidden'}>${hisEsc(s.error || '')}</div>
    ${body}`;
}

/** The search results, with the matched span marked and the tier printed. */
function searchResultsHtml(payload) {
  if (!payload || typeof payload !== 'object') return '';
  const data = normalizeSearch(payload);
  // No query and no hits is "nobody has searched", not "nothing matched" —
  // an empty-handed results panel over an empty query is a lie about a search
  // that never ran.
  if (!data.hits.length && !data.query) return '';
  if (!data.hits.length) {
    return `<div class="his-results" data-his-results>
      <div class="his-results-head">${tierChipHtml(data.tier, data.degraded)}</div>
      <div class="his-empty">Nothing matched “${hisEsc(data.query)}”.</div></div>`;
  }
  const rows = data.hits.map(hit => `<li class="his-hit">
    <button type="button" class="his-hit-open" data-his-open="${hisEsc(hit.conversation_id)}">
      <span class="his-hit-head">
        <span class="his-hit-title">${hisEsc(hit.title || 'Untitled')}</span>
        <span class="his-src is-${hisToken(hit.source)}">${hisEsc(sourceLabel(hit.source))}</span>
        <span class="his-hit-role">${hisEsc(hit.role)}</span>
        <span class="his-hit-date">${hisEsc(dateLabel(hit.ts))}</span>
      </span>
      <span class="his-hit-snippet">${highlightSnippet(hit)}</span>
    </button>
  </li>`).join('');
  return `<div class="his-results" data-his-results>
    <div class="his-results-head">
      <span class="his-results-count">${data.hits.length} of ${data.candidates} candidates</span>
      ${tierChipHtml(data.tier, data.degraded)}
    </div>
    <ul class="his-list">${rows}</ul></div>`;
}

/** The reader: one conversation, every message, in order. */
function conversationDetailHtml(payload) {
  const conv = normalizeDetail(payload);
  const messages = conv.messages.length
    ? conv.messages.map(message => `<div class="his-msg is-${hisToken(message.role)}">
        <div class="his-msg-head">
          <span class="his-msg-role">${hisEsc(message.role)}</span>
          <span class="his-msg-date">${hisEsc(dateLabel(message.ts))}</span>
        </div>
        <div class="his-msg-body">${hisEsc(message.content)}</div>
      </div>`).join('')
    : '<div class="his-empty">This conversation has no messages.</div>';
  return `<div class="his-detail">
    <button type="button" class="his-btn" data-his-back>← Back</button>
    <h3 class="his-detail-title">${hisEsc(conv.title || 'Untitled')}</h3>
    <p class="his-detail-meta">
      <span class="his-src is-${hisToken(conv.source)}">${hisEsc(sourceLabel(conv.source))}</span>
      <span>${hisEsc(dateLabel(conv.started_at))}</span>
      <span>${conv.message_count} messages</span>
      ${conv.model ? `<span>${hisEsc(conv.model)}</span>` : ''}
    </p>
    <div class="his-thread">${messages}</div>
  </div>`;
}
// ── History: end pure helpers ──

export {
  conversationListHtml, conversationDetailHtml, searchResultsHtml,
  importFormHtml, importPreviewHtml, highlightSnippet, normalizeSearch,
};

const $ = (id) => document.getElementById(id);

const MODAL_ID = 'history-modal';
const MAIN_ID = 'history-main';
const READER_ID = 'history-reader';

let _rows = [];
let _stats = null;
let _enabled = true;
let _wired = false;
let _returnFocus = null;
let _list = { source: '', query: '', error: '', loading: false };
let _import = { path: '', source: '', error: '', busy: false, preview: null, pending: null };
let _results = null;

/** fetch wrapper for /api/history/*: a non-2xx becomes an Error with {detail}. */
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

export const listHistory = (source) =>
  req(`/conversations?limit=500${source ? `&source=${encodeURIComponent(source)}` : ''}`);
export const readHistory = (id) => req(`/conversations/${encodeURIComponent(id)}`);
export const deleteHistory = (id) =>
  req(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
export const searchHistory = (q, source) =>
  req(`/search?q=${encodeURIComponent(q)}&k=25${source ? `&source=${encodeURIComponent(source)}` : ''}`);
export const historyStats = () => req('/stats');
export const importHistory = (body) =>
  req('/import', { method: 'POST', body: JSON.stringify(body) });

/** Multipart upload — the one call that must NOT send a JSON content type. */
async function uploadHistory(file, source, dryRun) {
  const form = new FormData();
  form.append('file', file, file.name);
  if (source) form.append('source', source);
  form.append('dry_run', dryRun ? '1' : '0');
  const res = await fetch(`${API}/import`, {
    method: 'POST', credentials: 'same-origin', body: form,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON body */ }
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
  return data;
}

// ── rendering ─────────────────────────────────────────────────────────────

function showView(which) {
  const main = $(MAIN_ID);
  const reader = $(READER_ID);
  if (main) main.classList.toggle('hidden', which !== 'main');
  if (reader) reader.classList.toggle('hidden', which !== 'reader');
}

function renderMain() {
  const host = $(MAIN_ID);
  if (!host) return;
  host.innerHTML = importFormHtml(_import)
    + importPreviewHtml(_import.preview)
    + conversationListHtml(_rows, { ..._list, stats: _stats, enabled: _enabled })
    + searchResultsHtml(_results);
  showView('main');
}

function inlineError(attribute, message) {
  const host = $(MAIN_ID);
  const box = host && host.querySelector(`[${attribute}]`);
  if (!box) return;
  box.textContent = String(message == null ? '' : message);
  box.hidden = !message;
}

export async function loadHistory(force = false) {
  if (_rows.length && !force) return _rows;
  _list.loading = true;
  _list.error = '';
  renderMain();
  try {
    const payload = await listHistory(_list.source);
    _rows = normalizeConversations(payload);
    _stats = normalizeStats(payload && payload.stats ? payload : payload);
    _enabled = !(payload && payload.enabled === false);
  } catch (error) {
    _list.error = `Could not read the imported history: ${error.message || error}`;
  } finally {
    _list.loading = false;
    renderMain();
  }
  return _rows;
}

async function runImport(dryRun) {
  const host = $(MAIN_ID);
  const pathBox = host && host.querySelector('[data-his-path]');
  const fileBox = host && host.querySelector('[data-his-file]');
  const sourceBox = host && host.querySelector('[data-his-source]');
  const file = fileBox && fileBox.files && fileBox.files[0];
  const path = String((pathBox && pathBox.value) || '').trim();
  const source = String((sourceBox && sourceBox.value) || '').trim();
  if (!file && !path) {
    _import.error = 'Give a path on this machine, or choose a file to upload.';
    renderMain();
    return;
  }
  _import.path = path;
  _import.source = source;
  _import.busy = true;
  _import.error = '';
  renderMain();
  try {
    const payload = file
      ? await uploadHistory(file, source, dryRun)
      : await importHistory({ path, source: source || null, dry_run: dryRun });
    _import.preview = payload;
    // An upload cannot be replayed from the file input after a re-render, so
    // the committed run keeps the file in hand for the second call.
    _import.pending = dryRun ? { file, path, source } : null;
    if (!dryRun) {
      _import.pending = null;
      uiModule.showToast?.(`Imported ${normalizeImport(payload).conversations} conversations`);
      await loadHistory(true);
      return;
    }
  } catch (error) {
    _import.error = `Import failed: ${error.message || error}`;
    _import.preview = null;
  } finally {
    _import.busy = false;
    renderMain();
  }
}

async function commitImport() {
  const pending = _import.pending;
  if (!pending) {
    _import.error = 'That preview is stale — run it again before importing.';
    renderMain();
    return;
  }
  _import.busy = true;
  _import.error = '';
  renderMain();
  try {
    const payload = pending.file
      ? await uploadHistory(pending.file, pending.source, false)
      : await importHistory({ path: pending.path, source: pending.source || null, dry_run: false });
    _import.preview = payload;
    _import.pending = null;
    uiModule.showToast?.(`Imported ${normalizeImport(payload).conversations} conversations`);
    await loadHistory(true);
  } catch (error) {
    _import.error = `Import failed: ${error.message || error}`;
  } finally {
    _import.busy = false;
    renderMain();
  }
}

async function openConversation(id) {
  const host = $(READER_ID);
  if (!host) return;
  try {
    const payload = await readHistory(id);
    host.innerHTML = conversationDetailHtml(payload);
    showView('reader');
  } catch (error) {
    inlineError('data-his-error', `Could not open that conversation: ${error.message || error}`);
  }
}

async function removeConversation(id) {
  const row = _rows.find(entry => entry.id === id);
  const name = row ? (row.title || 'this conversation') : 'this conversation';
  const ok = await uiModule.styledConfirm?.(
    `Delete "${name}"? The export it came from is on disk and is not touched.`,
    { confirmText: 'Delete', danger: true });
  if (!ok) return;
  try {
    await deleteHistory(id);
    await loadHistory(true);
  } catch (error) {
    inlineError('data-his-error', `Could not delete that conversation: ${error.message || error}`);
  }
}

async function runSearch() {
  const host = $(MAIN_ID);
  const box = host && host.querySelector('[data-his-query]');
  const query = String((box && box.value) || '').trim();
  _list.query = query;
  if (!query) {
    _results = null;
    renderMain();
    return;
  }
  try {
    _results = await searchHistory(query, _list.source);
    _list.error = '';
  } catch (error) {
    _results = null;
    _list.error = `Search failed: ${error.message || error}`;
  }
  renderMain();
}

// ── wiring: one delegated listener set on the modal ────────────────────────

function wire() {
  if (_wired) return;
  const modal = $(MODAL_ID);
  if (!modal) return;
  _wired = true;

  modal.addEventListener('click', (event) => {
    const target = event.target;
    if (target.closest('#close-history-modal')) { closeHistoryPanel(); return; }
    if (target.closest('[data-his-back]')) { showView('main'); return; }
    if (target.closest('[data-his-commit]')) { commitImport(); return; }
    if (target.closest('[data-his-cancel]')) {
      _import.preview = null;
      _import.pending = null;
      renderMain();
      return;
    }
    const del = target.closest('[data-his-delete]');
    if (del) { event.stopPropagation(); removeConversation(del.dataset.hisDelete); return; }
    const open = target.closest('[data-his-open]');
    if (open) openConversation(open.dataset.hisOpen);
  });

  modal.addEventListener('submit', (event) => {
    if (event.target.closest('[data-his-import-form]')) {
      event.preventDefault();
      runImport(true);
      return;
    }
    if (event.target.closest('[data-his-search-form]')) {
      event.preventDefault();
      runSearch();
    }
  });

  modal.addEventListener('change', (event) => {
    if (!event.target.matches('[data-his-filter]')) return;
    _list.source = event.target.value;
    _results = null;
    loadHistory(true);
  });

  $('tool-history-btn')?.addEventListener('click', () => openHistoryPanel());
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const confirmBox = $('styled-confirm-overlay');
    if (confirmBox && !confirmBox.classList.contains('hidden') && confirmBox.style.display !== 'none') return;
    const open = $(MODAL_ID);
    if (open && !open.classList.contains('hidden')) closeHistoryPanel();
  });
}

export async function openHistoryPanel(options = {}) {
  const modal = $(MODAL_ID);
  if (!modal) return;
  wire();
  _returnFocus = document.activeElement;
  modal.classList.remove('hidden');
  showView('main');
  await loadHistory(true);
  if (options.id) await openConversation(options.id);
}

export function closeHistoryPanel() {
  $(MODAL_ID)?.classList.add('hidden');
  try { _returnFocus?.focus?.(); } catch (_) {}
}

export function initHistoryImport() {
  wire();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initHistoryImport);
} else {
  initHistoryImport();
}

const historyImportModule = {
  initHistoryImport,
  openHistoryPanel,
  closeHistoryPanel,
  loadHistory,
  listHistory,
  readHistory,
  deleteHistory,
  searchHistory,
  historyStats,
  importHistory,
};

if (typeof window !== 'undefined') window.historyImportModule = historyImportModule;

export default historyImportModule;
