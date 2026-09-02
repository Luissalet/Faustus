// static/js/sysUsage.js
// Live system usage widget: what `ollama ps` and `nvidia-smi` would show,
// polled from /api/system/usage and rendered as a compact pill in the chat
// top bar (click to expand). Polls faster while a response is streaming.
//
// With more than one card (`gpu_pool.count > 1`) the pill and the panel have
// two views — `combined` (the pool: max util, summed VRAM, per-card rows
// underneath) and `separate` (one section per card, as with a single card)
// — chosen with a small segmented control in the panel and remembered in
// localStorage. The render helpers (pillText, gpuSectionsHtml, placementText)
// are pure so tests/test_sys_usage_js.py can run them under node.

let API_BASE = '';
let _pill = null;
let _panel = null;
let _timer = null;
let _visible = true;
let _expanded = false;
let _streaming = false;
let _last = null;
let _failures = 0;
let _gpuView = 'combined';

const VIS_KEY = 'odysseus-usage-visible';
const EXP_KEY = 'odysseus-usage-expanded';
const GPU_VIEW_KEY = 'odysseus-usage-gpu-view';
const IDLE_MS = 5000;
const BUSY_MS = 1500;

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function gb(bytes) { return bytes == null ? '—' : (bytes / 1073741824).toFixed(1); }
function mib2gb(mib) { return mib == null ? '—' : (mib / 1024).toFixed(1); }
// Whole gigabytes for a card's total (12282 MiB → "12"): the pill has to fit
// two cards where it used to fit one, so the totals lose their decimal.
function gbInt(mib) { return mib == null ? '—' : String(Math.round(mib / 1024)); }
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
function mbytes(bytes) { return bytes == null ? '—' : Math.round(bytes / 1048576).toLocaleString(); }
/** "NVIDIA GeForce RTX 4070 Ti" → "RTX 4070 Ti" where space is short. */
function shortGpuName(name) { return String(name || '').replace(/^NVIDIA GeForce /, ''); }

// ── the pool ────────────────────────────────────────────────────────────────

/** True when the payload describes more than one card. The multi-GPU views
 *  key off the server's pool block, not the length of `gpu[]`. */
function isMulti(d) {
  return !!(d && d.gpu_pool && d.gpu_pool.count > 1);
}

/** The pool block with every gap filled from the cards themselves, so a
 *  payload that carries only `gpu[]` still yields sane sums. */
export function poolOf(d) {
  const gpus = d && Array.isArray(d.gpu) ? d.gpu : [];
  const p = (d && d.gpu_pool) || {};
  const num = (v, fallback) => (v == null ? fallback : v);
  const has = k => gpus.some(g => g && g[k] != null);
  const sum = k => gpus.reduce((a, g) => a + (Number(g && g[k]) || 0), 0);
  const max = k => gpus.reduce((a, g) => (g && g[k] != null && (a == null || g[k] > a) ? g[k] : a), null);
  const count = Number(num(p.count, gpus.length)) || 0;
  return {
    count,
    util: num(p.util, max('util')),
    util_avg: num(p.util_avg, gpus.length && has('util') ? sum('util') / gpus.length : null),
    temp: num(p.temp, max('temp')),
    mem_used: num(p.mem_used, has('mem_used') ? sum('mem_used') : null),
    mem_total: num(p.mem_total, has('mem_total') ? sum('mem_total') : null),
    mem_free: num(p.mem_free, has('mem_free') ? sum('mem_free') : null),
    power: num(p.power, has('power') ? sum('power') : null),
    power_limit: num(p.power_limit, has('power_limit') ? sum('power_limit') : null),
    names: Array.isArray(p.names) ? p.names : gpus.map(g => (g && g.name) || ''),
  };
}

function gpuByIndex(d, idx) {
  const gpus = d && Array.isArray(d.gpu) ? d.gpu : [];
  return gpus.find(g => g && g.index === idx) || null;
}

/** `GPU 1 (RTX 5060 Ti)` / `split: #0 8.5 GB + #1 10.2 GB` / `CPU` / `—`
 *  for a loaded model's `placement` block; '' when the server sent none. */
export function placementText(m, d) {
  const p = m && m.placement;
  if (p == null) return '';
  if (p === 'cpu') return 'CPU';
  if (p === 'single') {
    const idx = Array.isArray(m.gpus) && m.gpus.length ? m.gpus[0] : null;
    if (idx == null) return 'GPU';
    const g = gpuByIndex(d, idx);
    const name = g && g.name ? shortGpuName(g.name) : '';
    return `GPU ${idx}${name ? ` (${name})` : ''}`;
  }
  if (p === 'split') {
    const parts = Array.isArray(m.per_gpu) && m.per_gpu.length
      ? m.per_gpu
      : (Array.isArray(m.gpus) ? m.gpus : []).map(i => ({ index: i }));
    if (!parts.length) return 'split';
    return `split: ${parts.map(x => `#${x.index}${x.bytes != null ? ` ${gb(x.bytes)} GB` : ''}`).join(' + ')}`;
  }
  return '—';
}

// ── DOM ─────────────────────────────────────────────────────────────────────

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
  // The combined / separate segmented control lives inside the re-rendered
  // panel HTML, so the click is delegated: switch the view and repaint, no
  // page reload, no closing of the panel.
  panel.addEventListener('click', (e) => {
    const btn = e.target && e.target.closest ? e.target.closest('[data-su-gpu-view]') : null;
    if (!btn || !panel.contains(btn)) return;
    e.preventDefault();
    e.stopPropagation();
    setGpuView(btn.getAttribute('data-su-gpu-view'));
  });
  document.addEventListener('click', (e) => {
    if (_expanded && !panel.contains(e.target) && e.target !== pill && !pill.contains(e.target)) setExpanded(false);
  });
  try { _visible = localStorage.getItem(VIS_KEY) !== '0'; } catch (_) {}
  try { _expanded = localStorage.getItem(EXP_KEY) === '1'; } catch (_) {}
  try { _gpuView = localStorage.getItem(GPU_VIEW_KEY) === 'separate' ? 'separate' : 'combined'; } catch (_) {}
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
/** 'combined' | 'separate' — how more than one card is shown. */
export function gpuView() { return _gpuView; }
export function setGpuView(mode) {
  _gpuView = mode === 'separate' ? 'separate' : 'combined';
  try { localStorage.setItem(GPU_VIEW_KEY, _gpuView); } catch (_) {}
  if (_pill) render();
}

async function fetchUsage() {
  const r = await fetch(`${API_BASE}/api/system/usage`, { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// ── the pill ────────────────────────────────────────────────────────────────

function _firstModel(d) {
  return (d && d.ollama && d.ollama.models && d.ollama.models[0]) || null;
}
function _spilling(d) {
  return !!(d && d.gpu_mem && d.gpu_mem.ollama && d.gpu_mem.ollama.spilling);
}

/** The pill's text. One card: `GPU 22% · 9.3/12.0G · 43° · qwen3.8 100%↑GPU · RAM 31%`.
 *  Two cards, combined: `GPU 22% · 9.3+10.6/28G · 43° · …` (util = pool max,
 *  VRAM = each card's used over the pool total, temp = max); separate:
 *  `GPU0 22% 9.3/12G · GPU1 0% 10.6/16G · 43° · …`. */
export function pillText(d, mode = 'combined') {
  if (!d) return 'usage: n/a';
  const gpus = Array.isArray(d.gpu) ? d.gpu : [];
  const gpu = gpus[0] || null;
  const model = _firstModel(d);
  const bits = [];
  // Weights paging into shared system memory is the one failure every other
  // gauge hides: VRAM, GPU% and `ollama ps` all keep reading healthy while the
  // model crawls over PCIe. Say it on the pill, not just in the panel.
  const spill = _spilling(d);
  if (isMulti(d) && gpus.length) {
    const pool = poolOf(d);
    if (mode === 'separate') {
      gpus.forEach((g, i) => {
        bits.push(`GPU${g.index != null ? g.index : i} ${pct(g.util)} ${mib2gb(g.mem_used)}/${gbInt(g.mem_total)}G`);
      });
    } else {
      bits.push(`GPU ${pct(pool.util)}`);
      bits.push(`${gpus.map(g => mib2gb(g.mem_used)).join('+')}/${gbInt(pool.mem_total)}G`);
    }
    if (pool.temp != null) bits.push(`${Math.round(pool.temp)}°`);
  } else if (gpu) {
    bits.push(`GPU ${pct(gpu.util)}`);
    bits.push(`${mib2gb(gpu.mem_used)}/${mib2gb(gpu.mem_total)}G`);
    if (gpu.temp != null) bits.push(`${Math.round(gpu.temp)}°`);
  }
  if (spill) bits.push('⚠ PCIe spill');
  if (model) bits.push(`${(model.name || '').split(':')[0]} ${model.gpu_pct}%↑GPU`);
  else if (d.ollama && d.ollama.reachable) bits.push('no model');
  else if (d.ollama) bits.push('ollama offline');
  if (d.ram && d.ram.total) bits.push(`RAM ${Math.round(d.ram.percent)}%`);
  return bits.join(' · ');
}

/** '' | 'warm' | 'hot' for the pill border: the busiest card decides (a card
 *  at 95 % while its neighbour idles is a full card, not a half-full pool). */
export function pillLevel(d) {
  const gpus = d && Array.isArray(d.gpu) ? d.gpu : [];
  let worst = 0;
  for (const g of gpus) {
    const vramPct = g && g.mem_total ? (g.mem_used / g.mem_total) * 100 : 0;
    worst = Math.max(worst, (g && g.util) || 0, vramPct || 0);
  }
  return meterClass(worst);
}

function render() {
  _ensureEls();
  if (!_pill) return;
  const d = _last;
  const text = _pill.querySelector('.su-text');
  const dot = _pill.querySelector('.su-dot');
  if (!d) { text.textContent = 'usage: n/a'; _pill.classList.add('unavailable'); return; }
  _pill.classList.remove('unavailable');
  const gpus = Array.isArray(d.gpu) ? d.gpu : [];
  const model = _firstModel(d);
  if (gpus.length) _pill.className = `sys-usage-pill ${pillLevel(d)}${_spilling(d) ? ' spill' : ''}`;
  text.textContent = pillText(d, _gpuView);
  // The busy dot: the pool's max util (one card = that card).
  dot.classList.toggle('busy', !!model && (gpus.length ? (poolOf(d).util || 0) > 5 : true));
  if (_panel && !_panel.hidden) renderPanel(d);
  try { if (window.modelControls && window.modelControls.noteUsage) window.modelControls.noteUsage(d); } catch (_) {}
}

// ── the panel ───────────────────────────────────────────────────────────────

function bar(label, value, total, unit, extra) {
  const p = total ? Math.max(0, Math.min(100, (value / total) * 100)) : 0;
  return `<div class="su-row"><span class="su-label">${esc(label)}</span>` +
    `<span class="su-meter ${meterClass(p)}"><span style="width:${p.toFixed(1)}%"></span></span>` +
    `<span class="su-val">${esc(extra != null ? extra : `${value}${unit || ''} / ${total}${unit || ''}`)}</span></div>`;
}

function _segmentedHtml(mode) {
  const on = m => (m === mode ? ' class="on"' : '');
  return `<span class="su-gpu-view" role="group" aria-label="GPU view">` +
    `<button type="button" data-su-gpu-view="combined"${on('combined')} title="One section for the pool, a row per card">Combined</button>` +
    `<button type="button" data-su-gpu-view="separate"${on('separate')} title="A section per card">Separate</button></span>`;
}

/** The models resident on one card, from `gpu[i].models`; a model that also
 *  appears on another card is marked "split with #N". */
function _cardModelsHtml(g, gpus) {
  const models = Array.isArray(g.models) ? g.models : [];
  if (!models.length) return `<div class="su-gpu-models su-muted">no model on this card</div>`;
  const lines = models.map(m => {
    const name = (m && m.name) || '';
    const others = gpus
      .filter(o => o && o !== g && o.index !== g.index && Array.isArray(o.models) && o.models.some(x => x && x.name === name))
      .map(o => `#${o.index}`);
    let line = esc(name || '?');
    if (m && m.bytes != null) line += ` · ${esc(gb(m.bytes))} GB`;
    if (others.length) line += ` · split with ${esc(others.join(', '))}`;
    return `<div>${line}</div>`;
  });
  return `<div class="su-gpu-models">${lines.join('')}</div>`;
}

/** A card as its own section — the single-card layout, unchanged; with more
 *  than one card the header is `#N <short name>` (the vendor prefix wrapped
 *  the header and squeezed the view switch to "Separat"), the models and
 *  (first card) the view switch. */
function _cardSectionHtml(g, gpus, extraHead, withModels) {
  const multi = gpus.length > 1;
  return `<div class="su-section"><div class="su-h${extraHead ? ' su-h-gpu' : ''}">${multi ? `#${g.index} ` : ''}${esc((multi ? shortGpuName(g.name) : g.name) || 'GPU')}${extraHead}</div>` +
    bar('Util', g.util || 0, 100, '%', `${pct(g.util)}`) +
    bar('VRAM', g.mem_used || 0, g.mem_total || 1, '', `${mib2gb(g.mem_used)} / ${mib2gb(g.mem_total)} GB`) +
    (g.power != null ? bar('Power', g.power, g.power_limit || g.power || 1, 'W', `${Math.round(g.power)} W${g.power_limit ? ' / ' + Math.round(g.power_limit) + ' W' : ''}`) : '') +
    (g.temp != null ? `<div class="su-row"><span class="su-label">Temp</span><span class="su-val">${Math.round(g.temp)} °C</span></div>` : '') +
    (withModels ? _cardModelsHtml(g, gpus) : '') +
    `</div>`;
}

/** The compact per-card row of the combined view:
 *  `#0 RTX 4070 Ti` [mini VRAM meter] `9.3/12 GB · 22 % · 39 ° · 17 W`. */
function _cardRowHtml(g, gpus) {
  const p = g.mem_total ? Math.max(0, Math.min(100, (g.mem_used / g.mem_total) * 100)) : 0;
  const stats = [`${mib2gb(g.mem_used)}/${gbInt(g.mem_total)} GB`];
  if (g.util != null) stats.push(`${Math.round(g.util)} %`);
  if (g.temp != null) stats.push(`${Math.round(g.temp)} °`);
  if (g.power != null) stats.push(`${Math.round(g.power)} W`);
  return `<div class="su-gpu-row" title="${esc(g.name || 'GPU')}">` +
    `<span class="su-gpu-name">#${g.index} ${esc(shortGpuName(g.name) || 'GPU')}</span>` +
    `<span class="su-gpu-mini ${meterClass(p)}"><span style="width:${p.toFixed(1)}%"></span></span>` +
    `<span class="su-val">${esc(stats.join(' · '))}</span></div>` +
    _cardModelsHtml(g, gpus);
}

/** The GPU part of the panel. One card: today's section. Several cards,
 *  combined: one "GPUs (N)" section with the pool bars and a compact row
 *  per card; separate: a section per card with its models. */
export function gpuSectionsHtml(d, mode = 'combined') {
  const gpus = d && Array.isArray(d.gpu) ? d.gpu : [];
  if (!gpus.length) {
    return `<div class="su-section"><div class="su-h">GPU</div><div class="su-muted">nvidia-smi unavailable${d && d.errors && d.errors.length ? ' — ' + esc(d.errors.join('; ')) : ''}</div></div>`;
  }
  if (!isMulti(d)) return gpus.map(g => _cardSectionHtml(g, gpus, '', false)).join('');
  const pool = poolOf(d);
  const seg = _segmentedHtml(mode === 'separate' ? 'separate' : 'combined');
  if (mode === 'separate') {
    return gpus.map((g, i) => _cardSectionHtml(g, gpus, i === 0 ? seg : '', true)).join('');
  }
  return `<div class="su-section"><div class="su-h su-h-gpu">GPUs (${pool.count})${seg}</div>` +
    bar('Util', pool.util || 0, 100, '%', `${pct(pool.util)} max${pool.util_avg != null ? ` · ${pct(pool.util_avg)} avg` : ''}`) +
    bar('VRAM', pool.mem_used || 0, pool.mem_total || 1, '', `${mib2gb(pool.mem_used)} / ${mib2gb(pool.mem_total)} GB`) +
    (pool.power != null ? bar('Power', pool.power, pool.power_limit || pool.power || 1, 'W', `${Math.round(pool.power)} W${pool.power_limit ? ' / ' + Math.round(pool.power_limit) + ' W' : ''}`) : '') +
    (pool.temp != null ? `<div class="su-row"><span class="su-label">Temp</span><span class="su-val">${Math.round(pool.temp)} °C max</span></div>` : '') +
    gpus.map(g => _cardRowHtml(g, gpus)).join('') +
    `</div>`;
}

/** The Ollama section: each loaded model with its GPU/CPU split, size,
 *  context, keep-alive and — when the server knows — where it sits. */
export function ollamaSectionHtml(d) {
  const o = (d && d.ollama) || {};
  const models = o.models || [];
  return `<div class="su-section"><div class="su-h">Ollama <span class="su-muted">${esc((o.base || '').replace(/^https?:\/\//, ''))}${o.reachable ? '' : ' · unreachable'}</span></div>` +
    (models.length ? models.map(m => `<div class="su-model"><div class="su-model-name">${esc(m.name)}<span class="su-muted"> ${esc(m.parameter_size || '')} ${esc(m.quantization || '')}</span></div>` +
      bar('GPU/CPU', m.gpu_pct, 100, '%', `${m.gpu_pct}% GPU / ${m.cpu_pct}% CPU`) +
      `<div class="su-row"><span class="su-label">Size</span><span class="su-val">${gb(m.size)} GB (${gb(m.size_vram)} GB in VRAM)</span></div>` +
      (m.placement != null ? `<div class="su-row"><span class="su-label">Placement</span><span class="su-val">${esc(placementText(m, d))}</span></div>` : '') +
      `<div class="su-row"><span class="su-label">Context</span><span class="su-val">${fmtCtx(m.context_length)} tokens</span></div>` +
      `<div class="su-row"><span class="su-label">Keep-alive</span><span class="su-val">${esc(untilText(m.expires_at)) || '—'}</span></div></div>`).join('')
      : `<div class="su-muted">${o.reachable ? 'No model loaded (ollama ps is empty).' : 'Cannot reach Ollama.'}</div>`) +
    `</div>`;
}

function renderPanel(d) {
  const rows = [];
  rows.push(gpuSectionsHtml(d, _gpuView));
  rows.push(ollamaSectionHtml(d));
  const gm = d.gpu_mem || {};
  if (gm.supported) {
    const om = gm.ollama || {};
    const fb = d.sysmem_fallback || {};
    const frac = Math.round((om.shared_fraction || 0) * 100);
    rows.push(`<div class="su-section"><div class="su-h">Shared GPU memory <span class="su-muted">system RAM, over PCIe</span></div>` +
      `<div class="su-row"><span class="su-label">Runner</span><span class="su-val">${mbytes(om.shared)} MB · ${frac}% of its GPU memory</span></div>` +
      `<div class="su-row"><span class="su-label">Everything</span><span class="su-val">${mbytes(gm.total_shared)} MB</span></div>` +
      (om.spilling
        ? `<div class="su-warn"><b>Weights are paging over PCIe.</b> The card ran out of room and the CUDA driver put part of the model in system memory instead of failing, so it is being read at ~25 GB/s instead of ~500. Nothing else shows this: VRAM, GPU% and <code>ollama ps</code> all still look healthy.` +
          (fb.steps && fb.steps.length ? `<div class="su-warn-steps">${fb.steps.map(s => `<div>· ${esc(s)}</div>`).join('')}</div>` : '') +
          `<div class="su-warn-steps"><div>· Or open the model settings and use <b>Fit to VRAM</b> to shrink the context instead.</div></div></div>`
        : `<div class="su-muted">A CUDA process always parks a few hundred MB here — that is not a spill.</div>`) +
      `</div>`);
  }
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

const sysUsage = { init, tick, toggle, setVisible, setExpanded, setGpuView, gpuView, get last() { return _last; } };
if (typeof window !== 'undefined') window.sysUsage = sysUsage;
export default sysUsage;
