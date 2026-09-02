// static/js/agentSettings.js — schema-driven "Agent & automation" settings form.
//
// Settings → Agent Tools → the "Agent & automation" card. The server describes
// every agent_* / browser_* / desktop_* key once (src/agent_settings_schema.py,
// GET /api/agent/settings/schema: {groups, defaults}); this module renders the
// groups into #agent-settings-root, reads the current values from
// GET /api/auth/settings and posts ONLY the keys that changed to
// POST /api/auth/settings, one group at a time.
//
// The rendering (renderSchemaHtml) and the value handling (parseFieldValue,
// formatFieldValue, valuesEqual, changedKeys, fieldMatches) are pure functions
// of (schema, values) so tests/test_agent_settings_js.py can run them under
// node without a DOM; bindAgentSettings() is the only part that touches
// document. The raw key sits under every label in monospace so slash-command
// users (/settings, the manage_settings tool) recognise what they are looking
// at.

import { invalidateSettings } from './appConfig.js';

const SCHEMA_URL = '/api/agent/settings/schema';
const SETTINGS_URL = '/api/auth/settings';
const ROOT_ID = 'agent-settings-root';

// Inputs that pre-date the generated form keep their ids so anything that
// still looks them up (older scripts, bookmarks in docs) finds the same control.
export const LEGACY_IDS = Object.freeze({
  agent_max_tool_calls: 'set-agentMaxTools',
  agent_max_rounds: 'set-agentMaxRounds',
});

const _ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, m => _ESC_MAP[m]); }

export function controlId(key) {
  return LEGACY_IDS[key] || ('agset-' + String(key).replace(/[^A-Za-z0-9_-]/g, '_'));
}

// ── values ──────────────────────────────────────────────────────────────────

/** The string (or boolean, for toggles) a control shows for `value`. */
export function formatFieldValue(field, value) {
  switch (field.type) {
    case 'bool':
      return value === true || value === 'true' || value === 1;
    case 'list':
      return Array.isArray(value) ? value.join(', ') : String(value == null ? '' : value);
    case 'int':
    case 'float':
      return value == null || value === '' ? '' : String(value);
    default:
      return value == null ? '' : String(value);
  }
}

/**
 * The setting value a control's raw reading (`checked` for toggles, `value`
 * otherwise) stands for. Numbers are clamped to the field's bounds and a
 * blank number falls back to `fallback` (the loaded value) so an emptied box
 * never posts NaN; a list is split on commas / newlines with blanks dropped.
 */
export function parseFieldValue(field, raw, fallback) {
  switch (field.type) {
    case 'bool':
      return raw === true || raw === 'true' || raw === 'on';
    case 'int':
    case 'float': {
      const text = String(raw == null ? '' : raw).trim();
      if (text === '') return fallback;
      let n = field.type === 'int' ? parseInt(text, 10) : parseFloat(text);
      if (Number.isNaN(n)) return fallback;
      if (typeof field.min === 'number' && n < field.min) n = field.min;
      if (typeof field.max === 'number' && n > field.max) n = field.max;
      return n;
    }
    case 'list':
      return String(raw == null ? '' : raw).split(/[,\n]/).map(s => s.trim()).filter(Boolean);
    default:
      return String(raw == null ? '' : raw).trim();
  }
}

export function valuesEqual(field, a, b) {
  if (field.type === 'list') {
    const x = Array.isArray(a) ? a : parseFieldValue(field, a);
    const y = Array.isArray(b) ? b : parseFieldValue(field, b);
    return x.length === y.length && x.every((v, i) => v === y[i]);
  }
  if (field.type === 'int' || field.type === 'float') {
    return Number(a) === Number(b);
  }
  if (field.type === 'bool') return !!a === !!b;
  return String(a == null ? '' : a) === String(b == null ? '' : b);
}

/** {key: value} for every field of `fields` whose current value differs from the loaded one. */
export function changedKeys(fields, loaded, current) {
  const out = {};
  for (const field of fields) {
    if (!(field.key in current)) continue;
    if (!valuesEqual(field, loaded[field.key], current[field.key])) out[field.key] = current[field.key];
  }
  return out;
}

export function fieldSearchText(field) {
  return [field.key, field.label, field.help].join(' ').toLowerCase();
}

export function fieldMatches(field, query) {
  const terms = String(query || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const hay = fieldSearchText(field);
  return terms.every(t => hay.includes(t));
}

function defaultLabel(field, value) {
  if (field.type === 'bool') return value ? 'on' : 'off';
  if (field.type === 'list') return Array.isArray(value) && value.length ? value.join(', ') : 'empty';
  if (value === '' || value == null) return 'empty';
  return String(value);
}

// ── rendering ───────────────────────────────────────────────────────────────

export function renderControlHtml(field, value) {
  const id = controlId(field.key);
  const common = `id="${esc(id)}" data-agset-key="${esc(field.key)}"`;
  switch (field.type) {
    case 'bool':
      return `<label class="admin-switch agset-switch"><input type="checkbox" ${common}${formatFieldValue(field, value) ? ' checked' : ''}><span class="admin-slider"></span></label>`;
    case 'int':
    case 'float': {
      const attrs = ['min', 'max', 'step']
        .filter(a => typeof field[a] === 'number')
        .map(a => ` ${a}="${esc(field[a])}"`).join('');
      return `<input type="number" class="settings-select agset-input agset-number" ${common}${attrs} value="${esc(formatFieldValue(field, value))}" inputmode="decimal">`;
    }
    case 'select': {
      const cur = String(value == null ? '' : value);
      const opts = (field.options || []).map(o =>
        `<option value="${esc(o.value)}"${o.value === cur ? ' selected' : ''}>${esc(o.label || o.value)}</option>`).join('');
      return `<select class="settings-select agset-input agset-select" ${common}>${opts}</select>`;
    }
    case 'secret':
      return `<input type="password" class="settings-select agset-input" ${common} value="${esc(formatFieldValue(field, value))}" autocomplete="off" spellcheck="false"${field.placeholder ? ` placeholder="${esc(field.placeholder)}"` : ''}>`;
    case 'list':
      return `<input type="text" class="settings-select agset-input agset-list" ${common} value="${esc(formatFieldValue(field, value))}" spellcheck="false" placeholder="${esc(field.placeholder || 'comma-separated')}">`;
    default:
      return `<input type="text" class="settings-select agset-input" ${common} value="${esc(formatFieldValue(field, value))}" spellcheck="false"${field.placeholder ? ` placeholder="${esc(field.placeholder)}"` : ''}>`;
  }
}

export function renderFieldHtml(field, value, defaultValue) {
  const id = controlId(field.key);
  const isDefault = valuesEqual(field, value, defaultValue);
  const hint = field.restart_hint
    ? '<div class="agset-hint agset-hint-restart">Restart needed to apply.</div>'
    : '';
  return `<div class="agset-field${isDefault ? '' : ' is-changed'}" data-agset-key="${esc(field.key)}" data-agset-type="${esc(field.type)}" data-agset-search="${esc(fieldSearchText(field))}">
  <div class="agset-main">
    <label class="settings-label agset-label" for="${esc(id)}">${esc(field.label)}</label>
    <code class="agset-key">${esc(field.key)}</code>
    <div class="admin-toggle-sub agset-help">${esc(field.help)}</div>${hint}
  </div>
  <div class="agset-control">
    ${renderControlHtml(field, value)}
    <button type="button" class="admin-btn-sm agset-reset" data-agset-key="${esc(field.key)}" title="Reset to default: ${esc(defaultLabel(field, defaultValue))}" aria-label="Reset ${esc(field.label)} to default">↺</button>
  </div>
</div>`;
}

export function renderGroupHtml(group, values, defaults) {
  const fields = (group.fields || []).map(f => renderFieldHtml(f, values[f.key], defaults[f.key])).join('\n');
  return `<section class="agset-group" data-agset-group="${esc(group.id)}">
  <div class="agset-group-head">
    <div class="agset-group-text">
      <div class="agset-group-title">${esc(group.title)}</div>
      <div class="admin-toggle-sub agset-group-help">${esc(group.help)}</div>
    </div>
    <div class="agset-group-actions">
      <span class="agset-group-msg" data-agset-msg="${esc(group.id)}"></span>
      <button type="button" class="admin-btn-add agset-save" data-agset-save="${esc(group.id)}" disabled>Save</button>
    </div>
  </div>
  <div class="agset-fields">
${fields}
  </div>
</section>`;
}

export function renderSchemaHtml(schema, values) {
  const groups = schema.groups || [];
  const defaults = schema.defaults || {};
  const total = groups.reduce((n, g) => n + (g.fields || []).length, 0);
  return `<div class="agset-toolbar">
  <input type="search" id="agset-search" class="settings-select agset-search" placeholder="Filter settings (key, label, help)…" autocomplete="off" spellcheck="false" aria-label="Filter agent settings">
  <span class="agset-count" data-agset-count>${total} settings</span>
</div>
<div class="agset-groups">
${groups.map(g => renderGroupHtml(g, values, defaults)).join('\n')}
</div>
<div class="admin-empty agset-empty hidden" data-agset-empty>No settings match.</div>`;
}

// ── DOM wiring ──────────────────────────────────────────────────────────────

/**
 * Render `schema` with `values` into `root` and wire search, dirty tracking,
 * per-field reset and per-group save. `post(payload)` must resolve to the
 * saved settings object (what POST /api/auth/settings returns). Returns the
 * controller so a later refresh can replace the loaded values.
 */
export function bindAgentSettings(root, schema, values, { post } = {}) {
  const defaults = schema.defaults || {};
  const fieldsByKey = new Map();
  const groupFields = new Map();
  for (const g of schema.groups || []) {
    groupFields.set(g.id, g.fields || []);
    for (const f of g.fields || []) fieldsByKey.set(f.key, f);
  }
  const loaded = {};
  for (const key of fieldsByKey.keys()) loaded[key] = key in values ? values[key] : defaults[key];

  root.innerHTML = renderSchemaHtml(schema, loaded);

  const control = key => root.querySelector(`[data-agset-key="${key}"].agset-input, input[type="checkbox"][data-agset-key="${key}"]`);
  const fieldEl = key => root.querySelector(`.agset-field[data-agset-key="${key}"]`);

  function readKey(key) {
    const field = fieldsByKey.get(key);
    const el = control(key);
    if (!field || !el) return loaded[key];
    return parseFieldValue(field, field.type === 'bool' ? el.checked : el.value, loaded[key]);
  }

  function writeKey(key, value) {
    const field = fieldsByKey.get(key);
    const el = control(key);
    if (!field || !el) return;
    if (field.type === 'bool') el.checked = formatFieldValue(field, value);
    else el.value = formatFieldValue(field, value);
  }

  function currentFor(groupId) {
    const cur = {};
    for (const f of groupFields.get(groupId) || []) cur[f.key] = readKey(f.key);
    return cur;
  }

  function syncField(key) {
    const field = fieldsByKey.get(key);
    const el = fieldEl(key);
    if (!field || !el) return;
    const value = readKey(key);
    el.classList.toggle('is-dirty', !valuesEqual(field, value, loaded[key]));
    el.classList.toggle('is-changed', !valuesEqual(field, value, defaults[key]));
  }

  function syncGroup(groupId) {
    const changed = Object.keys(changedKeys(groupFields.get(groupId) || [], loaded, currentFor(groupId)));
    const btn = root.querySelector(`[data-agset-save="${groupId}"]`);
    if (btn) {
      btn.disabled = changed.length === 0;
      btn.textContent = changed.length ? `Save ${changed.length}` : 'Save';
    }
  }

  function groupOf(key) {
    for (const [gid, fields] of groupFields) if (fields.some(f => f.key === key)) return gid;
    return null;
  }

  function onEdit(e) {
    const key = e.target && e.target.dataset ? e.target.dataset.agsetKey : null;
    if (!key || !fieldsByKey.has(key)) return;
    syncField(key);
    const gid = groupOf(key);
    if (gid) syncGroup(gid);
  }
  root.addEventListener('input', onEdit);
  root.addEventListener('change', onEdit);

  root.addEventListener('click', async e => {
    const reset = e.target.closest ? e.target.closest('.agset-reset') : null;
    if (reset) {
      const key = reset.dataset.agsetKey;
      writeKey(key, defaults[key]);
      syncField(key);
      const gid = groupOf(key);
      if (gid) syncGroup(gid);
      return;
    }
    const save = e.target.closest ? e.target.closest('[data-agset-save]') : null;
    if (save) await saveGroup(save.dataset.agsetSave);
  });

  function setMsg(groupId, text, isError) {
    const msg = root.querySelector(`[data-agset-msg="${groupId}"]`);
    if (!msg) return;
    msg.textContent = text;
    msg.classList.toggle('is-error', !!isError);
    if (text && !isError) setTimeout(() => { if (msg.textContent === text) msg.textContent = ''; }, 3000);
  }

  async function saveGroup(groupId) {
    const fields = groupFields.get(groupId) || [];
    const payload = changedKeys(fields, loaded, currentFor(groupId));
    const keys = Object.keys(payload);
    if (!keys.length) return;
    const btn = root.querySelector(`[data-agset-save="${groupId}"]`);
    if (btn) btn.disabled = true;
    try {
      const saved = typeof post === 'function' ? await post(payload) : null;
      for (const key of keys) {
        loaded[key] = saved && key in saved ? saved[key] : payload[key];
        writeKey(key, loaded[key]);   // the server may have clamped it
        syncField(key);
      }
      const restart = fields.some(f => f.key in payload && f.restart_hint);
      setMsg(groupId, `Saved ${keys.length} setting${keys.length === 1 ? '' : 's'}${restart ? ' — restart needed' : ''}`);
    } catch (err) {
      setMsg(groupId, 'Failed to save: ' + ((err && err.message) || err), true);
    }
    syncGroup(groupId);
  }

  const searchEl = root.querySelector('#agset-search');
  const emptyEl = root.querySelector('[data-agset-empty]');
  const countEl = root.querySelector('[data-agset-count]');
  const total = fieldsByKey.size;
  function applyFilter() {
    const q = searchEl ? searchEl.value : '';
    let shown = 0;
    for (const [gid, fields] of groupFields) {
      let visible = 0;
      for (const f of fields) {
        const el = fieldEl(f.key);
        if (!el) continue;
        const ok = fieldMatches(f, q);
        el.classList.toggle('hidden', !ok);
        if (ok) visible++;
      }
      const sec = root.querySelector(`[data-agset-group="${gid}"]`);
      if (sec) sec.classList.toggle('hidden', visible === 0);
      shown += visible;
    }
    if (emptyEl) emptyEl.classList.toggle('hidden', shown > 0);
    if (countEl) countEl.textContent = q.trim() ? `${shown} of ${total} settings` : `${total} settings`;
  }
  if (searchEl) searchEl.addEventListener('input', applyFilter);

  for (const gid of groupFields.keys()) syncGroup(gid);

  return {
    /** Replace the loaded values with fresh server state; untouched fields follow, edited ones keep their edit. */
    refresh(nextValues) {
      for (const [key, field] of fieldsByKey) {
        if (!(key in nextValues)) continue;
        const dirty = !valuesEqual(field, readKey(key), loaded[key]);
        loaded[key] = nextValues[key];
        if (!dirty) writeKey(key, loaded[key]);
        syncField(key);
      }
      for (const gid of groupFields.keys()) syncGroup(gid);
    },
    hasUnsavedChanges() {
      for (const gid of groupFields.keys()) {
        if (Object.keys(changedKeys(groupFields.get(gid), loaded, currentFor(gid))).length) return true;
      }
      return false;
    },
    applyFilter,
    read: readKey,
  };
}

// ── page entry point ────────────────────────────────────────────────────────

let _schemaPromise = null;
let _controller = null;
let _boundRoot = null;

async function _fetchJson(url) {
  const r = await fetch(url, { credentials: 'same-origin' });
  if (!r.ok) {
    const err = new Error(r.status === 403 ? 'Admin only' : `HTTP ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

async function _postSettings(payload) {
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (_) { /* no body */ }
      throw new Error(detail || `HTTP ${r.status}`);
    }
    return r.json();
  } finally {
    // Same rule as settings.js: every writer drops the shared snapshot.
    invalidateSettings();
  }
}

/**
 * Render (first call) or refresh (later calls) the form in #agent-settings-root.
 * The schema is fetched once per page; values are re-read on every call so the
 * panel shows authoritative state, without clobbering an edit in progress.
 */
export async function loadAgentSettings() {
  const root = document.getElementById(ROOT_ID);
  if (!root) return null;
  try {
    if (!_schemaPromise) {
      _schemaPromise = _fetchJson(SCHEMA_URL).catch(err => { _schemaPromise = null; throw err; });
    }
    const [schema, values] = await Promise.all([_schemaPromise, _fetchJson(SETTINGS_URL)]);
    if (_controller && _boundRoot === root) {
      _controller.refresh(values);
    } else {
      _controller = bindAgentSettings(root, schema, values, { post: _postSettings });
      _boundRoot = root;
    }
    return _controller;
  } catch (err) {
    if (!_controller || _boundRoot !== root) {
      root.innerHTML = `<div class="admin-empty">${esc(err && err.status === 403 ? 'Admin only.' : 'Could not load agent settings: ' + ((err && err.message) || err))}</div>`;
    }
    return null;
  }
}

const agentSettings = { loadAgentSettings, bindAgentSettings, renderSchemaHtml, LEGACY_IDS };
export default agentSettings;
