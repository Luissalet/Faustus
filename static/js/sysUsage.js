// static/js/sysUsage.js
// Live system usage widget: what `ollama ps` and `nvidia-smi` would show,
// polled from /api/system/usage and rendered as a compact pill in the chat
// top bar (click to expand). Polls faster while a response is streaming.

let API_BASE = '';
let _pill = null;
let _panel = null;
let _timer = null;
let _visible = true;
let _expanded = false;
let _streaming = false;
let _last = null;
let _failures = 0;

const VIS_KEY = 'odysseus-usage-visible';
const EXP_KEY = 'odysseus-usage-expanded';
const IDLE_MS = 5000;
const BUSY_MS = 1500;

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function gb(bytes) { return bytes == null ? '—' : (bytes / 1073741824).toFixed(1); }
function mib2gb(mib) { return mib == null ? '—' : (mib / 1024).toFixed(1); }
function fmtCtx(n) { if (!n) return '—'; return n >= 1024 ? `${Math.round(n / 1024)}k` : String(n); }
function pct(v) { return v == null ? '—' : `${Math.round(v)}%`; }
function untilText(iso) {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '';
  const s = Math.round((t - Date.now()) / 1000);
  if (s <= 0) return 'unloading';
  if (s < 90) return `${s}s left`;
  if (s < 3600) return `${Math.round(s / 60)} min left`;
  return `${(s / 3600).toFixed(1)} h left`;
}
function meterClass(p) { return p == null ? '' : p >= 90 ? 'hot' : p >= 70 ? 'warm' : ''; }

function _ensureEls() {
  if (_pill && document.body.contains(_pill)) return;
  const bar = document.querySelector('.chat-top-bar');
  if (!bar) return;
  const pill = document.createElement('button');
  pill.type = 'button';
  pill.id = 'sys-usage-pill';
  pill.className = 'sys-usage-pill';
  pill.title = 'Live usage: GPU · VRAM · loaded model · RAM (click to expand)';
  pill.innerHTML = '<span class="su-dot"></span><span class="su-text">usage…</span>';
  pill.addEventListener('click', (e) => { e.stopPropagation(); setExpanded(!_expanded); });
  const overlay = bar.querySelector('.chat-meta-overlay');
  if (overlay) bar.insertBefore(pill, overlay); else bar.appendChild(pill);
  _pill = pill;

  const panel = document.createElement('div');
  panel.id = 'sys-usage-panel';
  panel.className = 'sys-usage-panel';
  panel.hidden = true;
  const host = document.getElementById('chat-container') || document.body;
  host.appendChild(panel);
  _panel = panel;
  document.addEventListener('click', (e) => {
    if (_expanded && !panel.contains(e.target) && e.target !== pill && !pill.contains(e.target)) setExpanded(false);
  });
  try { _visible = localStorage.getItem(VIS_KEY) !== '0'; } catch (_) {}
  try { _expanded = localStorage.getItem(EXP_KEY) === '1'; } catch (_) {}
  pill.hidden = !_visible;
  panel.hidden = !(_visible && _expanded);
}

export function setVisible(v) {
  _visible = !!v;
  try { localStorage.setItem(VIS_KEY, _visible ? '1' : '0'); } catch (_) {}
  if (_pill) _pill.hidden = !_visible;
  if (_panel) _panel.hidden = !(_visible && _expanded);
  if (_visible) tick();
}
export function toggle() { setVisible(!_visible); }
export function setExpanded(v) {
  _expanded = !!v;
  try { localStorage.setItem(EXP_KEY, _expanded ? '1' : '0'); } catch (_) {}
  if (_panel) _panel.hidden = !(_visible && _expanded);
  if (_expanded) render();
}

async function fetchUsage() {
  const r = await fetch(`${API_BASE}/api/system/usage`, { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function render() {
  _ensureEls();
  if (!_pill) return;
  const d = _last;
  const text = _pill.querySelector('.su-text');
  const dot = _pill.querySelector('.su-dot');
  if (!d) { text.textContent = 'usage: n/a'; _pill.classList.add('unavailable'); return; }
  _pill.classList.remove('unavailable');
  const gpu = (d.gpu && d.gpu[0]) || null;
  const model = (d.ollama && d.ollama.models && d.ollama.models[0]) || null;
  const bits = [];
  if (gpu) {
    const vramPct = gpu.mem_total ? (gpu.mem_used / gpu.mem_total) * 100 : null;
    bits.push(`GPU ${pct(gpu.util)}`);
    bits.push(`${mib2gb(gpu.mem_used)}/${mib2gb(gpu.mem_total)}G`);
    if (gpu.temp != null) bits.push(`${Math.round(gpu.temp)}°`);
    _pill.className = `sys-usage-pill ${meterClass(Math.max(gpu.util || 0, vramPct || 0))}`;
  }
  if (model) bits.push(`${(model.name || '').split(':')[0]} ${model.gpu_pct}%↑GPU`);
  else if (d.ollama && d.ollama.reachable) bits.push('no model');
  else if (d.ollama) bits.push('ollama offline');
  if (d.ram && d.ram.total) bits.push(`RAM ${Math.round(d.ram.percent)}%`);
  text.textContent = bits.join(' · ');
  dot.classList.toggle('busy', !!model && (gpu ? (gpu.util || 0) > 5 : true));
  if (_panel && !_panel.hidden) renderPanel(d);
  try { if (window.modelControls && window.modelControls.noteUsage) window.modelControls.noteUsage(d); } catch (_) {}
}

function bar(label, value, total, unit, extra) {
  const p = total ? Math.max(0, Math.min(100, (value / total) * 100)) : 0;
  return `<div class="su-row"><span class="su-label">${esc(label)}</span>` +
    `<span class="su-meter ${meterClass(p)}"><span style="width:${p.toFixed(1)}%"></span></span>` +
    `<span class="su-val">${esc(extra != null ? extra : `${value}${unit || ''} / ${total}${unit || ''}`)}</span></div>`;
}

function renderPanel(d) {
  const rows = [];
  const gpus = d.gpu || [];
  if (!gpus.length) rows.push(`<div class="su-section"><div class="su-h">GPU</div><div class="su-muted">nvidia-smi unavailable${d.errors && d.errors.length ? ' — ' + esc(d.errors.join('; ')) : ''}</div></div>`);
  for (const g of gpus) {
    rows.push(`<div class="su-section"><div class="su-h">${esc(g.name || 'GPU')}${gpus.length > 1 ? ` #${g.index}` : ''}</div>` +
      bar('Util', g.util || 0, 100, '%', `${pct(g.util)}`) +
      bar('VRAM', g.mem_used || 0, g.mem_total || 1, '', `${mib2gb(g.mem_used)} / ${mib2gb(g.mem_total)} GB`) +
      (g.power != null ? bar('Power', g.power, g.power_limit || g.power || 1, 'W', `${Math.round(g.power)} W${g.power_limit ? ' / ' + Math.round(g.power_limit) + ' W' : ''}`) : '') +
      (g.temp != null ? `<div class="su-row"><span class="su-label">Temp</span><span class="su-val">${Math.round(g.temp)} °C</span></div>` : '') +
      `</div>`);
  }
  const o = d.ollama || {};
  const models = o.models || [];
  rows.push(`<div class="su-section"><div class="su-h">Ollama <span class="su-muted">${esc((o.base || '').replace(/^https?:\/\//, ''))}${o.reachable ? '' : ' · unreachable'}</span></div>` +
    (models.length ? models.map(m => `<div class="su-model"><div class="su-model-name">${esc(m.name)}<span class="su-muted"> ${esc(m.parameter_size || '')} ${esc(m.quantization || '')}</span></div>` +
      bar('GPU/CPU', m.gpu_pct, 100, '%', `${m.gpu_pct}% GPU / ${m.cpu_pct}% CPU`) +
      `<div class="su-row"><span class="su-label">Size</span><span class="su-val">${gb(m.size)} GB (${gb(m.size_vram)} GB in VRAM)</span></div>` +
      `<div class="su-row"><span class="su-label">Context</span><span class="su-val">${fmtCtx(m.context_length)} tokens</span></div>` +
      `<div class="su-row"><span class="su-label">Keep-alive</span><span class="su-val">${esc(untilText(m.expires_at)) || '—'}</span></div></div>`).join('')
      : `<div class="su-muted">${o.reachable ? 'No model loaded (ollama ps is empty).' : 'Cannot reach Ollama.'}</div>`) +
    `</div>`);
  if (d.ram && d.ram.total) {
    rows.push(`<div class="su-section"><div class="su-h">Host</div>` +
      bar('RAM', d.ram.used, d.ram.total, '', `${gb(d.ram.used)} / ${gb(d.ram.total)} GB (${pct(d.ram.percent)})`) +
      (d.cpu && d.cpu.percent != null ? bar('CPU', d.cpu.percent, 100, '%', `${pct(d.cpu.percent)}${d.cpu.count ? ' · ' + d.cpu.count + ' threads' : ''}`) : '') +
      `</div>`);
  }
  rows.push(`<div class="su-foot su-muted">updated ${new Date((d.ts || Date.now() / 1000) * 1000).toLocaleTimeString()} · polling every ${(_streaming ? BUSY_MS : IDLE_MS) / 1000}s${_streaming ? ' (streaming)' : ''} · <code>/usage off</code> to hide</div>`);
  _panel.innerHTML = rows.join('');
}

async function tick() {
  if (!_visible) return;
  try {
    _last = await fetchUsage();
    _failures = 0;
  } catch (e) {
    _failures += 1;
    if (_failures > 3) _last = null;
  }
  render();
}

function schedule() {
  if (_timer) clearInterval(_timer);
  _timer = setInterval(tick, _streaming ? BUSY_MS : IDLE_MS);
}

export function init(apiBase) {
  API_BASE = apiBase || '';
  _ensureEls();
  tick();
  schedule();
  window.addEventListener('odysseus:chat-busy-change', (ev) => {
    const active = !!(ev.detail && ev.detail.active);
    if (active !== _streaming) { _streaming = active; schedule(); if (active) tick(); }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
}

const sysUsage = { init, tick, toggle, setVisible, setExpanded, get last() { return _last; } };
window.sysUsage = sysUsage;
export default sysUsage;
