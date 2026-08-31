// static/js/modelControls.js
// "What is the model doing right now?" — a pill next to the model picker that
// shows the effective generation settings of the active chat (temperature,
// max tokens, thinking, context) and a popover to pin per-session overrides.
// Overrides travel with every request as the `gen_overrides` form field
// (routes/chat_routes.py::_parse_gen_overrides) and win over the preset.
//
// Slash commands (wired from slashCommands.js via window.modelControls):
//   /temp 0.3      /maxtokens 8192     /topp 0.9     /think on|off|auto
//   /gen           (show)               /gen reset
//
// Round facts (finish_reason, effective temperature, whether thinking was
// requested) arrive through the `round_info` SSE event → noteRoundInfo().

let API_BASE = '';
let _getSessionId = () => null;
let _pill = null;
let _pop = null;
let _lastRound = null;          // last round_info payload
let _modelInfoCache = new Map(); // model → /api/system/ollama/model response
let _usageSnapshot = null;       // last /api/system/usage payload (shared by sysUsage.js)

const STORE_KEY = 'odysseus-gen-overrides';
const GLOBAL = '*';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ── storage ──────────────────────────────────────────────────────────────────

function _loadAll() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {}; } catch (_) { return {}; }
}
function _saveAll(all) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(all)); } catch (_) {}
}

export function getOverridesForSession(sessionId) {
  const all = _loadAll();
  const merged = Object.assign({}, all[GLOBAL] || {}, sessionId ? (all[sessionId] || {}) : {});
  // Drop empty/auto values.
  for (const k of Object.keys(merged)) {
    if (merged[k] === null || merged[k] === undefined || merged[k] === '' || merged[k] === 'auto') delete merged[k];
  }
  return merged;
}

export function setOverride(key, value, { sessionId = _getSessionId(), global = false } = {}) {
  const all = _loadAll();
  const scope = global || !sessionId ? GLOBAL : sessionId;
  all[scope] = all[scope] || {};
  if (value === null || value === undefined || value === '' || value === 'auto') delete all[scope][key];
  else all[scope][key] = value;
  if (!Object.keys(all[scope]).length) delete all[scope];
  _saveAll(all);
  refresh();
}

export function resetOverrides({ sessionId = _getSessionId(), global = false } = {}) {
  const all = _loadAll();
  if (global) delete all[GLOBAL];
  if (sessionId) delete all[sessionId];
  _saveAll(all);
  refresh();
}

// ── data ─────────────────────────────────────────────────────────────────────

export function noteRoundInfo(json) {
  _lastRound = json;
  refresh();
}

export function noteUsage(snapshot) {
  _usageSnapshot = snapshot;
  refresh();
}

function _currentModel() {
  const label = document.getElementById('model-picker-label');
  const t = label ? (label.title || label.textContent || '') : '';
  return String(t || '').trim();
}

async function _modelInfo(model) {
  if (!model) return null;
  if (_modelInfoCache.has(model)) return _modelInfoCache.get(model);
  try {
    const r = await fetch(`${API_BASE}/api/system/ollama/model/${encodeURIComponent(model)}`, { credentials: 'same-origin' });
    if (!r.ok) { _modelInfoCache.set(model, null); return null; }
    const data = await r.json();
    _modelInfoCache.set(model, data);
    return data;
  } catch (_) { _modelInfoCache.set(model, null); return null; }
}

function _loadedModelEntry(model) {
  const models = (_usageSnapshot && _usageSnapshot.ollama && _usageSnapshot.ollama.models) || [];
  if (!models.length) return null;
  const short = (model || '').split('/').pop();
  return models.find(m => m.name === model || m.name === short || (short && m.name && m.name.startsWith(short))) || models[0];
}

function _effective(sessionId) {
  const ov = getOverridesForSession(sessionId);
  const r = _lastRound || {};
  return {
    temperature: ov.temperature != null ? ov.temperature : (r.temperature != null ? r.temperature : null),
    temperatureSource: ov.temperature != null ? 'pinned' : (r.temperature != null ? (r.temperature_capped ? 'capped' : 'preset') : 'preset'),
    max_tokens: ov.max_tokens != null ? ov.max_tokens : (r.max_tokens != null ? r.max_tokens : null),
    top_p: ov.top_p != null ? ov.top_p : null,
    think: ov.think != null ? ov.think : (r.think != null ? r.think : 'auto'),
    finish_reason: r.finish_reason || null,
    round: r.round || null,
    tools_sent: r.tools_sent != null ? r.tools_sent : null,
    native_tools: r.native_tools,
  };
}

// ── UI ───────────────────────────────────────────────────────────────────────

function _ensurePill() {
  if (_pill && document.body.contains(_pill)) return _pill;
  const wrap = document.getElementById('model-picker-wrap');
  if (!wrap) return null;
  const pill = document.createElement('button');
  pill.type = 'button';
  pill.id = 'model-controls-pill';
  pill.className = 'model-controls-pill';
  pill.title = 'Model settings for this chat (temperature, max tokens, thinking)';
  pill.innerHTML = '<span class="mc-icon">🎛</span><span class="mc-text">model</span>';
  // Sit right next to the model picker button (the wrap is absolutely
  // positioned top-right of the composer; the dropdown menu is absolute too,
  // so a flex row only lays out the two buttons).
  wrap.classList.add('has-model-controls');
  wrap.insertBefore(pill, wrap.firstChild);
  pill.addEventListener('click', (e) => { e.stopPropagation(); togglePopover(); });
  _pill = pill;
  return pill;
}

function _fmtTemp(v) { return v == null ? 'auto' : Number(v).toFixed(2).replace(/0$/, ''); }
function _fmtTok(v) { if (v == null) return '—'; if (!v) return '∞'; return v >= 1024 ? `${Math.round(v / 1024)}k` : String(v); }

export function refresh() {
  const pill = _ensurePill();
  if (!pill) return;
  const sid = _getSessionId();
  const eff = _effective(sid);
  const loaded = _loadedModelEntry(_currentModel());
  const bits = [`T ${_fmtTemp(eff.temperature)}${eff.temperatureSource === 'pinned' ? '📌' : ''}`];
  if (eff.think !== 'auto') bits.push(eff.think ? 'think on' : 'think off');
  if (loaded && loaded.context_length) bits.push(`ctx ${_fmtTok(loaded.context_length)}`);
  if (eff.finish_reason && eff.finish_reason !== 'stop' && eff.finish_reason !== 'tool_calls') bits.push(eff.finish_reason);
  pill.querySelector('.mc-text').textContent = bits.join(' · ');
  pill.classList.toggle('has-pinned', Object.keys(getOverridesForSession(sid)).length > 0);
  if (_pop && !_pop.hidden) _renderPopover();
}

function _ensurePopover() {
  if (_pop && document.body.contains(_pop)) return _pop;
  const pop = document.createElement('div');
  pop.id = 'model-controls-pop';
  pop.className = 'model-controls-pop';
  pop.hidden = true;
  document.body.appendChild(pop);
  document.addEventListener('click', (e) => {
    if (!pop.hidden && !pop.contains(e.target) && e.target !== _pill && !(_pill && _pill.contains(e.target))) pop.hidden = true;
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !pop.hidden) pop.hidden = true; });
  _pop = pop;
  return pop;
}

export function togglePopover(force) {
  const pop = _ensurePopover();
  const show = force != null ? force : pop.hidden;
  pop.hidden = !show;
  if (show) { _renderPopover(); _position(); }
}

function _position() {
  if (!_pop || !_pill) return;
  const r = _pill.getBoundingClientRect();
  const w = Math.min(360, window.innerWidth - 16);
  _pop.style.width = w + 'px';
  let left = Math.min(Math.max(8, r.right - w), window.innerWidth - w - 8);
  _pop.style.left = left + 'px';
  // Above the composer.
  _pop.style.top = 'auto';
  _pop.style.bottom = Math.max(8, window.innerHeight - r.top + 8) + 'px';
}

function _renderPopover() {
  const pop = _ensurePopover();
  const sid = _getSessionId();
  const ov = getOverridesForSession(sid);
  const eff = _effective(sid);
  const model = _currentModel();
  const loaded = _loadedModelEntry(model);
  const info = _modelInfoCache.get(model) || null;
  if (!_modelInfoCache.has(model)) _modelInfo(model).then(() => { if (!pop.hidden) _renderPopover(); });
  const caps = info && Array.isArray(info.capabilities) ? info.capabilities : [];
  const supportsThinking = caps.includes('thinking');
  const supportsTools = caps.includes('tools');
  const tempVal = ov.temperature != null ? ov.temperature : (eff.temperature != null ? eff.temperature : 0.4);
  const rows = [];
  rows.push(`<div class="mc-row mc-head"><b>${esc(model || 'model')}</b>` +
    (loaded ? `<span class="mc-muted">loaded · ${loaded.gpu_pct}% GPU / ${loaded.cpu_pct}% CPU · ctx ${_fmtTok(loaded.context_length)}${loaded.quantization ? ' · ' + esc(loaded.quantization) : ''}</span>` : `<span class="mc-muted">not loaded${_usageSnapshot && _usageSnapshot.ollama && !_usageSnapshot.ollama.reachable ? ' · Ollama unreachable' : ''}</span>`) +
    `</div>`);
  if (info) {
    rows.push(`<div class="mc-row mc-caps">` +
      `<span class="mc-cap ${supportsTools ? 'on' : ''}">tools ${supportsTools ? '✓' : '✗'}</span>` +
      `<span class="mc-cap ${supportsThinking ? 'on' : ''}">thinking ${supportsThinking ? '✓' : '✗'}</span>` +
      (info.context_length ? `<span class="mc-cap">max ctx ${_fmtTok(info.context_length)}</span>` : '') +
      (info.parameter_size ? `<span class="mc-cap">${esc(info.parameter_size)}</span>` : '') +
      `</div>`);
  }
  if (_lastRound) {
    rows.push(`<div class="mc-row mc-muted">last round #${eff.round}: finish_reason=<b>${esc(eff.finish_reason || '?')}</b>` +
      (eff.tools_sent != null ? ` · ${eff.tools_sent} tool schemas${eff.native_tools === false ? ' (text mode)' : ''}` : '') +
      (_lastRound.temperature != null ? ` · used T ${_fmtTemp(_lastRound.temperature)}${_lastRound.temperature_capped ? ' (capped from ' + _fmtTemp(_lastRound.temperature_capped) + ')' : ''}` : '') +
      `</div>`);
  }
  rows.push(`<label class="mc-row"><span>Temperature <b id="mc-temp-val">${_fmtTemp(tempVal)}</b> <span class="mc-muted">${ov.temperature != null ? 'pinned' : (eff.temperatureSource === 'capped' ? 'capped for local agent' : 'preset / auto')}</span></span>` +
    `<input type="range" id="mc-temp" min="0" max="2" step="0.05" value="${Number(tempVal)}"></label>`);
  rows.push(`<label class="mc-row"><span>Max tokens <span class="mc-muted">(0 = unlimited)</span></span>` +
    `<input type="number" id="mc-maxtok" min="0" max="262144" step="256" value="${ov.max_tokens != null ? ov.max_tokens : (eff.max_tokens != null ? eff.max_tokens : 0)}"></label>`);
  rows.push(`<label class="mc-row"><span>Top-p <span class="mc-muted">(blank = server default)</span></span>` +
    `<input type="number" id="mc-topp" min="0.05" max="1" step="0.05" value="${ov.top_p != null ? ov.top_p : ''}" placeholder="auto"></label>`);
  const think = ov.think != null ? (ov.think ? 'on' : 'off') : 'auto';
  rows.push(`<div class="mc-row"><span>Thinking${supportsThinking ? '' : info ? ' <span class="mc-muted">(model does not support it)</span>' : ''}</span>` +
    `<div class="mc-seg" id="mc-think">` +
    ['auto', 'on', 'off'].map(v => `<button type="button" data-v="${v}" class="${think === v ? 'on' : ''}">${v}</button>`).join('') +
    `</div></div>`);
  rows.push(`<div class="mc-row mc-actions"><button type="button" id="mc-reset" class="mc-btn">Reset to preset</button>` +
    `<label class="mc-muted"><input type="checkbox" id="mc-global"> apply to all chats</label></div>`);
  rows.push(`<div class="mc-row mc-muted mc-hint">Slash: <code>/temp 0.3</code> · <code>/maxtokens 8192</code> · <code>/topp 0.9</code> · <code>/think on|off|auto</code> · <code>/gen reset</code></div>`);
  pop.innerHTML = rows.join('');

  const isGlobal = () => !!(pop.querySelector('#mc-global') && pop.querySelector('#mc-global').checked);
  const temp = pop.querySelector('#mc-temp');
  temp.addEventListener('input', () => { pop.querySelector('#mc-temp-val').textContent = _fmtTemp(temp.value); });
  temp.addEventListener('change', () => setOverride('temperature', Number(temp.value), { global: isGlobal() }));
  const mt = pop.querySelector('#mc-maxtok');
  mt.addEventListener('change', () => setOverride('max_tokens', mt.value === '' ? null : Math.max(0, parseInt(mt.value, 10) || 0), { global: isGlobal() }));
  const tp = pop.querySelector('#mc-topp');
  tp.addEventListener('change', () => setOverride('top_p', tp.value === '' ? null : Number(tp.value), { global: isGlobal() }));
  pop.querySelector('#mc-think').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-v]');
    if (!b) return;
    const v = b.dataset.v;
    setOverride('think', v === 'auto' ? null : (v === 'on'), { global: isGlobal() });
  });
  pop.querySelector('#mc-reset').addEventListener('click', () => resetOverrides({ global: isGlobal() }));
}

// ── slash commands ───────────────────────────────────────────────────────────

export function handleCommand(name, args) {
  const a = (args[0] || '').toLowerCase();
  const sid = _getSessionId();
  switch (name) {
    case 'temp': case 'temperature': {
      if (!a || a === 'show') return `Temperature: <b>${esc(_fmtTemp(_effective(sid).temperature))}</b> (${esc(_effective(sid).temperatureSource)}).`;
      if (a === 'auto' || a === 'reset') { setOverride('temperature', null); return 'Temperature: back to preset / auto.'; }
      const v = Number(a);
      if (!Number.isFinite(v) || v < 0 || v > 2) return 'Usage: <code>/temp 0.3</code> (0–2) or <code>/temp auto</code>.';
      setOverride('temperature', v);
      return `Temperature pinned to <b>${esc(_fmtTemp(v))}</b> for this chat.`;
    }
    case 'maxtokens': case 'max_tokens': {
      if (!a || a === 'show') return `Max tokens: <b>${esc(_fmtTok(_effective(sid).max_tokens))}</b>.`;
      if (a === 'auto' || a === 'reset') { setOverride('max_tokens', null); return 'Max tokens: back to preset.'; }
      const v = parseInt(a, 10);
      if (!Number.isFinite(v) || v < 0) return 'Usage: <code>/maxtokens 8192</code> (0 = unlimited).';
      setOverride('max_tokens', v);
      return `Max tokens pinned to <b>${v || '∞'}</b> for this chat.`;
    }
    case 'topp': case 'top_p': {
      if (!a || a === 'show') return `Top-p: <b>${esc(_effective(sid).top_p ?? 'auto')}</b>.`;
      if (a === 'auto' || a === 'reset') { setOverride('top_p', null); return 'Top-p: server default.'; }
      const v = Number(a);
      if (!Number.isFinite(v) || v <= 0 || v > 1) return 'Usage: <code>/topp 0.9</code> (0–1).';
      setOverride('top_p', v);
      return `Top-p pinned to <b>${v}</b>.`;
    }
    case 'think': case 'thinking': {
      if (!a || a === 'show') { const t = _effective(sid).think; return `Thinking: <b>${t === 'auto' ? 'auto' : (t ? 'on' : 'off')}</b>.`; }
      if (a === 'auto' || a === 'reset') { setOverride('think', null); return 'Thinking: auto (Odysseus decides per model).'; }
      if (a === 'on' || a === 'off') { setOverride('think', a === 'on'); return `Thinking <b>${a}</b> for this chat (applies to models that support it).`; }
      return 'Usage: <code>/think on|off|auto</code>.';
    }
    case 'gen': case 'model-settings': {
      if (a === 'reset') { resetOverrides(); return 'Generation overrides cleared for this chat.'; }
      if (a === 'open') { togglePopover(true); return ''; }
      const ov = getOverridesForSession(sid);
      const eff = _effective(sid);
      return `Model: <b>${esc(_currentModel())}</b> · T ${esc(_fmtTemp(eff.temperature))} (${esc(eff.temperatureSource)}) · max ${esc(_fmtTok(eff.max_tokens))} · top-p ${esc(eff.top_p ?? 'auto')} · thinking ${eff.think === 'auto' ? 'auto' : (eff.think ? 'on' : 'off')}` +
        (eff.finish_reason ? ` · last finish_reason ${esc(eff.finish_reason)}` : '') +
        `<br><span class="mc-muted">pinned: ${Object.keys(ov).length ? esc(JSON.stringify(ov)) : 'none'} · <code>/gen open</code> for the panel</span>`;
    }
  }
  return null;
}

export function init(apiBase, deps = {}) {
  API_BASE = apiBase || '';
  if (deps.getSessionId) _getSessionId = deps.getSessionId;
  _ensurePill();
  refresh();
  document.addEventListener('odysseus:session-switch', () => { _lastRound = null; refresh(); });
  window.addEventListener('resize', () => { if (_pop && !_pop.hidden) _position(); });
  // Picker label changes (model switch) → re-render.
  const label = document.getElementById('model-picker-label');
  if (label && window.MutationObserver) {
    new MutationObserver(() => refresh()).observe(label, { childList: true, characterData: true, subtree: true, attributes: true });
  }
}

const modelControls = { init, refresh, getOverridesForSession, setOverride, resetOverrides, noteRoundInfo, noteUsage, togglePopover, handleCommand };
window.modelControls = modelControls;
export default modelControls;
