// static/js/localModels.js — Settings → Local models (the Ollama model manager).
//
// What LM Studio's My Models / Discover / loaded-models screens do, for the
// Ollama endpoints this install is configured against, on top of
// routes/local_models_routes.py:
//
//   GET  /api/local-models?endpoint_id=      installed + loaded + the card + fit
//   POST /api/local-models/pull?stream=false → {pull:{id}}; progress arrives on
//        GET /api/local-models/pulls/{id}/events (EventSource), and an open
//        page re-attaches to whatever /pulls still lists — a pull is a server
//        job, closing the tab does not stop it
//   POST /api/local-models/load|unload, DELETE /api/local-models/{name}
//   PUT  /api/local-models/{name}/options    num_ctx / num_gpu / keep_alive / main_gpu
//   GET  /api/local-models/discover?q=       curated catalogue with fit badges
//
// With more than one card the `vram` block carries the pool (`count`,
// `gpus[]`), the loaded rows say where each model sits (`placement`,
// `gpus`, `per_gpu`), a fit verdict can be `split` (bigger than any one
// card, fits the pool) and the options form offers `main_gpu` to pin a
// model to a card (the top-level `gpus[]` lists them).
//
// The render functions are pure (HTML in, HTML out) so
// tests/test_local_models_js.py can run them under node without a DOM;
// activate()/deactivate() and the click handling are the only parts that
// touch document. Visible to every signed-in user; the buttons that change
// something only render for admins (window._isAdmin), and the server
// enforces the same line.

import uiModule from './ui.js';
import { invalidateSettings } from './appConfig.js';

const API = '/api/local-models';
const POLL_MS = 8000;
const GIB = 1073741824;

const _ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, m => _ESC_MAP[m]); }
function attr(s) { return esc(s); }

// ── formatting ──────────────────────────────────────────────────────────────

export function fmtGb(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return '—';
  if (n < 0.95 * GIB) return `${Math.round(n / 1048576)} MB`;
  return `${(n / GIB).toFixed(1)} GB`;
}

export function fmtCtx(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '—';
  return v >= 1024 ? `${Math.round(v / 1024)}k` : String(v);
}

export function untilText(iso, now = Date.now()) {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '';
  const s = Math.round((t - now) / 1000);
  if (s > 10 * 365 * 86400) return 'kept loaded';
  if (s <= 0) return 'unloading';
  if (s < 90) return `${s}s left`;
  if (s < 3600) return `${Math.round(s / 60)} min left`;
  return `${(s / 3600).toFixed(1)} h left`;
}

export function fmtDate(iso) {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '';
  try { return new Date(t).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }); }
  catch (_) { return String(iso).slice(0, 10); }
}

/** "NVIDIA GeForce RTX 4070 Ti" → "RTX 4070 Ti" where the card is named in
 *  a chip or an option label. */
export function shortGpuName(name) { return String(name == null ? '' : name).replace(/^NVIDIA GeForce /, ''); }

/** `GPU 0 — RTX 4070 Ti (12 GB)`: the label of a card in the main_gpu select. */
export function gpuOptionLabel(g) {
  const total = Number(g && g.total_bytes) || 0;
  const name = shortGpuName(g && g.name) || 'GPU';
  return `GPU ${g && g.index != null ? g.index : '?'} — ${name}${total ? ` (${Math.round(total / GIB)} GB)` : ''}`;
}

const FIT_WORD = { fits: 'fits', tight: 'tight', over: 'no fit', split: 'split' };

/** Size + fit verdict, the same three states (and colours) as the picker,
 *  plus `split`: bigger than any one card, fits the pool, so Ollama spreads
 *  it across the cards. */
export function fitBadgeHtml(fit, sizeBytes) {
  let state = fit && fit.state ? String(fit.state) : '';
  if (fit && fit.split && state && state !== 'over') state = 'split';
  const size = fmtGb(sizeBytes);
  const word = FIT_WORD[state] || '';
  const title = (fit && fit.note) || `${size} on disk. Approximate — the KV cache grows on top of it with the context window.`;
  const cls = state ? ` lm-fit-${state}` : '';
  return `<span class="lm-fit${cls}" title="${attr(title)}">${esc(word ? `${size} · ${word}` : size)}</span>`;
}

const CAP_LABELS = [
  ['vision', 'vision', 'Accepts images'],
  ['tools', 'tools', 'Native tool calling'],
  ['thinking', 'think', 'Reasoning / thinking mode'],
  ['embedding', 'embed', 'Embedding model (no chat)'],
];

/** `caps` is either the {vision,tools,…} object of an installed model or the
 *  capability list of a catalogue entry. */
export function capsHtml(caps) {
  const set = new Set(Array.isArray(caps) ? caps : Object.keys(caps || {}).filter(k => caps[k]));
  return CAP_LABELS
    .filter(([key]) => set.has(key))
    .map(([key, label, title]) => `<span class="lm-cap lm-cap-${key}" title="${attr(title)}">${esc(label)}</span>`)
    .join('');
}

// ── sections ────────────────────────────────────────────────────────────────

/** One card of the pool (`vram.gpus[]`): its own models / other / free bar
 *  under the pool bar, with the models resident on it. `models_bytes` null
 *  (not measurable on this platform) → a single "used" segment. */
function _vramGpuHtml(g) {
  const total = Number(g.total_bytes) || 0;
  const used = Math.max(0, Number(g.used_bytes) || 0);
  const measured = g.models_bytes != null;
  const models = measured ? Math.max(0, Number(g.models_bytes) || 0) : 0;
  const others = measured ? Math.max(0, Number(g.other_bytes != null ? g.other_bytes : used - models) || 0) : 0;
  const free = Math.max(0, total - used);
  const pct = v => (total ? Math.max(0, Math.min(100, 100 * v / total)) : 0);
  const names = (g.models || []).filter(Boolean).join(', ');
  const idx = g.index != null ? g.index : '?';
  const segs = measured
    ? `<span class="lm-vram-seg lm-vram-models" style="width:${pct(models).toFixed(1)}%" title="Models on this card${names ? ': ' + attr(names) : ''}"></span>` +
      `<span class="lm-vram-seg lm-vram-other" style="width:${pct(others).toFixed(1)}%" title="Other processes on this card"></span>`
    : `<span class="lm-vram-seg lm-vram-used" style="width:${pct(used).toFixed(1)}%" title="In use on this card${names ? ' (' + attr(names) + ')' : ''} — per-model bytes are not measurable here"></span>`;
  const label = measured
    ? `GPU ${idx}: ${fmtGb(models)} models, ${fmtGb(others)} other, ${fmtGb(free)} free`
    : `GPU ${idx}: ${fmtGb(used)} used, ${fmtGb(free)} free`;
  return `
    <div class="lm-vram-gpu" data-lm-gpu="${attr(idx)}">
      <div class="lm-vram-head">
        <span class="lm-vram-name">GPU ${esc(idx)} · ${esc(shortGpuName(g.name) || 'GPU')}</span>
        <span class="lm-vram-nums">${esc(fmtGb(used))} of ${esc(fmtGb(total))} used · ${esc(fmtGb(free))} free</span>
      </div>
      <div class="lm-vram-bar" role="img" aria-label="${attr(label)}">${segs}</div>
      <div class="lm-vram-gpu-models lm-muted">${names ? esc(names) : 'nothing loaded on this card'}${g.budget_bytes != null ? ` · budget ${esc(fmtGb(g.budget_bytes))}` : ''}</div>
    </div>`;
}

export function renderVramHtml(vram, loaded = []) {
  if (!vram || !vram.supported) {
    const why = vram && vram.reason ? ` ${esc(vram.reason)}` : '';
    return `<div class="lm-vram-none">No VRAM reading for this endpoint.${why}</div>`;
  }
  const total = Number(vram.total_bytes) || 0;
  const runner = Number(vram.held_by_runner_bytes) || 0;
  const others = Number(vram.other_bytes) || 0;
  const pct = v => (total ? Math.max(0, Math.min(100, 100 * v / total)) : 0);
  const free = Math.max(0, total - runner - others);
  const names = (loaded || []).map(m => m.name).filter(Boolean).join(', ');
  const count = Number(vram.count) || 0;
  const cards = count > 1 && Array.isArray(vram.gpus) ? vram.gpus : [];
  const multi = count > 1;
  return `
    <div class="lm-vram-head">
      <span class="lm-vram-name">${esc(vram.name || 'GPU')}${multi ? ` · ${count} GPUs` : ''}</span>
      <span class="lm-vram-nums">${esc(fmtGb(runner + others))} of ${esc(fmtGb(total))} used · ${esc(fmtGb(free))} free</span>
    </div>
    <div class="lm-vram-bar" role="img" aria-label="VRAM: ${attr(fmtGb(runner))} models, ${attr(fmtGb(others))} other, ${attr(fmtGb(free))} free">
      <span class="lm-vram-seg lm-vram-models" style="width:${pct(runner).toFixed(1)}%" title="Models loaded by Ollama${names ? ': ' + attr(names) : ''}"></span>
      <span class="lm-vram-seg lm-vram-other" style="width:${pct(others).toFixed(1)}%" title="Other processes on the card"></span>
    </div>
    <div class="lm-vram-legend">
      <span><i class="lm-vram-dot lm-vram-models"></i>models ${esc(fmtGb(runner))}</span>
      <span><i class="lm-vram-dot lm-vram-other"></i>other ${esc(fmtGb(others))}</span>
      <span title="CUDA context, cuBLAS workspace and compute buffers, gone before a single weight is loaded${multi ? ' — one per card' : ''}.">reserve ${esc(fmtGb(vram.reserve_bytes))}${multi ? ` × ${count}` : ''}</span>
      <span title="What a model's weights can take right now, KV cache not included${multi ? ' — across the pool' + (vram.largest_single_budget_bytes != null ? '; a single card takes up to ' + attr(fmtGb(vram.largest_single_budget_bytes)) : '') : ''}.">budget ${esc(fmtGb(vram.budget_bytes))}</span>
    </div>${cards.map(_vramGpuHtml).join('')}${multi ? `
    <div class="lm-vram-note lm-muted">Ollama places each model on the card with the most free memory and splits a model across cards only when it does not fit one; pin a card per model in Options… (main_gpu). OLLAMA_SCHED_SPREAD=1 on the Ollama server spreads every model.</div>` : ''}`;
}

/** Where a loaded model sits, as a chip: `GPU 1 · RTX 5060 Ti`,
 *  `split #0 8.5 GB + #1 10.2 GB`, `CPU`; '' when the server did not say
 *  (an older server, or placement unknown). `gpus` is the card list
 *  (`[{index, name, total_bytes}]`) used to name the card. */
export function placementChipHtml(m, gpus = []) {
  const p = m && m.placement;
  if (!p || p === 'unknown') return '';
  const cardName = idx => {
    const g = (gpus || []).find(x => x && x.index === idx);
    return g ? shortGpuName(g.name) : '';
  };
  if (p === 'cpu') {
    return `<span class="lm-place lm-place-cpu" title="No weights on a GPU: the model runs on the CPU.">CPU</span>`;
  }
  if (p === 'single') {
    const idx = Array.isArray(m.gpus) && m.gpus.length ? m.gpus[0] : null;
    if (idx == null) return '';
    const name = cardName(idx);
    const text = `GPU ${idx}${name ? ` · ${name}` : ''}`;
    return `<span class="lm-place lm-place-single" title="Resident on GPU ${attr(idx)}${name ? ' (' + attr(name) + ')' : ''}: the card with the most free memory when it loaded, or the card its main_gpu option pins.">${esc(text)}</span>`;
  }
  if (p === 'split') {
    const parts = Array.isArray(m.per_gpu) && m.per_gpu.length
      ? m.per_gpu
      : (Array.isArray(m.gpus) ? m.gpus : []).map(i => ({ index: i }));
    if (!parts.length) return '';
    const text = `split ${parts.map(x => `#${x.index}${x.bytes != null ? ` ${fmtGb(x.bytes)}` : ''}`).join(' + ')}`;
    return `<span class="lm-place lm-place-split" title="Bigger than any one card: Ollama split the weights across ${parts.length} GPUs (${attr(parts.map(x => `#${x.index}${cardName(x.index) ? ' ' + cardName(x.index) : ''}`).join(', '))}). Layers cross the PCIe bus between the cards, slower than a single card but far faster than the CPU.">${esc(text)}</span>`;
  }
  return '';
}

export function renderLoadedHtml(loaded, { isAdmin = false, endpointId = '', gpus = [] } = {}) {
  if (!loaded || !loaded.length) {
    return '<div class="admin-empty">Nothing is loaded right now.</div>';
  }
  const rows = loaded.map(m => {
    const gpu = Number(m.gpu_pct) || 0;
    const spill = gpu < 100 && Number(m.size_cpu) > 0;
    const split = spill
      ? `<span class="lm-split lm-split-spill" title="${attr(fmtGb(m.size_cpu))} of the weights are in system RAM — expect PCIe paging and a fraction of the speed.">${gpu}% GPU · ${100 - gpu}% CPU</span>`
      : '<span class="lm-split">100% GPU</span>';
    const place = placementChipHtml(m, gpus);
    const ctx = m.context_length ? `<span class="lm-muted" title="Context window it was loaded with">ctx ${esc(fmtCtx(m.context_length))}</span>` : '';
    const until = untilText(m.expires_at);
    const unload = isAdmin
      ? `<button type="button" class="admin-btn-sm" data-lm-action="unload" data-lm-name="${attr(m.name)}" title="Evict from VRAM now (keep_alive 0)">Unload</button>`
      : '';
    return `
      <div class="lm-loaded-row" data-lm-loaded="${attr(m.name)}">
        <div class="lm-loaded-main">
          <span class="lm-name">${esc(m.name)}</span>
          <span class="lm-muted">${esc(fmtGb(m.size))} resident · ${esc(fmtGb(m.size_vram))} VRAM</span>
          ${place ? place + '\n          ' : ''}${split}
          ${ctx}
          ${until ? `<span class="lm-muted lm-until" title="${attr(m.expires_at)}">${esc(until)}</span>` : ''}
        </div>
        ${unload}
      </div>`;
  });
  return `<div class="lm-loaded" data-endpoint="${attr(endpointId)}">${rows.join('')}</div>`;
}

function _optionsSummary(opts) {
  if (!opts || !Object.keys(opts).length) return '';
  const bits = [];
  if (opts.num_ctx != null) bits.push(`ctx ${fmtCtx(opts.num_ctx)}`);
  if (opts.num_gpu != null) bits.push(`gpu ${opts.num_gpu}`);
  if (opts.main_gpu != null && opts.main_gpu !== '') bits.push(`gpu #${opts.main_gpu}`);
  if (opts.keep_alive != null && opts.keep_alive !== '') bits.push(`keep ${opts.keep_alive}`);
  return bits.join(' · ');
}

/** The main_gpu select: Auto (value "", the server clears the key) and one
 *  option per card. Rendered when there is a choice to make — more than one
 *  card, or a pin already saved that the admin may want to clear. */
function _mainGpuSelectHtml(o, gpus) {
  const cards = (gpus || []).filter(g => g && g.index != null);
  const raw = o.main_gpu == null || o.main_gpu === '' ? null : Number(o.main_gpu);
  const saved = Number.isFinite(raw) ? raw : null;
  if (cards.length < 2 && saved == null) return '';
  const opts = [`<option value=""${saved == null ? ' selected' : ''}>Auto — Ollama picks the freest card, splits when needed</option>`];
  cards.forEach(g => {
    opts.push(`<option value="${attr(g.index)}"${saved === Number(g.index) ? ' selected' : ''}>${esc(gpuOptionLabel(g))}</option>`);
  });
  if (saved != null && !cards.some(g => Number(g.index) === saved)) {
    opts.push(`<option value="${attr(saved)}" selected>GPU ${esc(saved)} — not listed on this endpoint</option>`);
  }
  return `
      <label>main_gpu<span class="lm-muted"> (pin to a card)</span>
        <select name="main_gpu">${opts.join('')}</select>
      </label>`;
}

export function renderOptionsFormHtml(model, gpus = []) {
  const o = (model && model.options) || {};
  const name = model && model.name ? model.name : '';
  const maxCtx = model && model.context_length ? ` (model max ${fmtCtx(model.context_length)})` : '';
  return `
    <form class="lm-options-form" data-lm-options-form="${attr(name)}">
      <label>num_ctx<span class="lm-muted">${esc(maxCtx)}</span>
        <input type="number" name="num_ctx" min="512" max="1048576" step="512" placeholder="model default" value="${attr(o.num_ctx == null ? '' : o.num_ctx)}">
      </label>
      <label>num_gpu<span class="lm-muted"> (layers on the GPU)</span>
        <input type="number" name="num_gpu" min="0" max="1024" step="1" placeholder="auto" value="${attr(o.num_gpu == null ? '' : o.num_gpu)}">
      </label>${_mainGpuSelectHtml(o, gpus)}
      <label>keep_alive<span class="lm-muted"> (5m, 1h, -1 = forever)</span>
        <input type="text" name="keep_alive" placeholder="5m" value="${attr(o.keep_alive == null ? '' : o.keep_alive)}">
      </label>
      <div class="lm-options-actions">
        <button type="submit" class="admin-btn-sm" data-lm-action="save-options" data-lm-name="${attr(name)}">Save</button>
        <button type="button" class="admin-btn-sm" data-lm-action="close-options" data-lm-name="${attr(name)}">Cancel</button>
        <span class="lm-muted">Applied to every request for this model on this endpoint, under anything the chat sets explicitly (/ctx, model controls).</span>
      </div>
    </form>`;
}

export function renderInstalledHtml(models, { isAdmin = false, endpointId = '', canSetDefault = true, optionsOpen = '', gpus = [] } = {}) {
  if (!models || !models.length) {
    return '<div class="admin-empty">No models installed on this endpoint yet — pull one below.</div>';
  }
  const head = `
    <div class="lm-row lm-head">
      <span class="lm-c-name">Model</span>
      <span class="lm-c-size">Size · fit</span>
      <span class="lm-c-meta">Quant · params</span>
      <span class="lm-c-caps">Caps</span>
      <span class="lm-c-ctx">Ctx</span>
      <span class="lm-c-actions"></span>
    </div>`;
  const rows = models.map(m => {
    const summary = _optionsSummary(m.options);
    const actions = [];
    if (isAdmin) {
      if (m.loaded) {
        actions.push(`<button type="button" class="admin-btn-sm" data-lm-action="unload" data-lm-name="${attr(m.name)}" title="Evict from VRAM">Unload</button>`);
      } else {
        actions.push(`<button type="button" class="admin-btn-sm" data-lm-action="load" data-lm-name="${attr(m.name)}" data-lm-embedding="${m.capabilities && m.capabilities.embedding ? '1' : '0'}" title="Warm it up now (keep_alive from its options, else 5m)">Load</button>`);
      }
      if (canSetDefault && !(m.capabilities && m.capabilities.embedding)) {
        actions.push(`<button type="button" class="admin-btn-sm" data-lm-action="default" data-lm-name="${attr(m.name)}" title="Make this the default chat model (Settings → AI Defaults)">Set default</button>`);
      }
      actions.push(`<button type="button" class="admin-btn-sm${summary ? ' lm-has-options' : ''}" data-lm-action="options" data-lm-name="${attr(m.name)}" title="num_ctx / num_gpu / keep_alive / main_gpu defaults for this model">Options…</button>`);
      actions.push(`<button type="button" class="admin-btn-sm lm-danger" data-lm-action="delete" data-lm-name="${attr(m.name)}" title="Remove the model files from this Ollama">Delete</button>`);
    }
    const family = m.family || (m.families && m.families[0]) || '';
    const sub = [family, m.license, fmtDate(m.modified_at)].filter(Boolean).join(' · ');
    const form = optionsOpen && optionsOpen === m.name ? renderOptionsFormHtml(m, gpus) : '';
    return `
      <div class="lm-row${m.loaded ? ' lm-loaded-now' : ''}" data-lm-model="${attr(m.name)}">
        <span class="lm-c-name">
          <span class="lm-name" title="${attr(m.digest ? 'digest ' + m.digest : m.name)}">${esc(m.name)}</span>
          ${m.loaded ? '<span class="lm-pill lm-pill-loaded" title="Resident in memory now">loaded</span>' : ''}
          ${sub ? `<span class="lm-sub">${esc(sub)}</span>` : ''}
          ${summary ? `<span class="lm-sub lm-options-summary" title="Saved load options">${esc(summary)}</span>` : ''}
        </span>
        <span class="lm-c-size">${fitBadgeHtml(m.fit, m.size)}</span>
        <span class="lm-c-meta">${esc([m.quantization, m.parameter_size].filter(Boolean).join(' · ') || '—')}</span>
        <span class="lm-c-caps">${capsHtml(m.capabilities) || '<span class="lm-muted">—</span>'}</span>
        <span class="lm-c-ctx" title="Context length the model was trained for (from /api/show)">${esc(fmtCtx(m.context_length))}</span>
        <span class="lm-c-actions">${actions.join('')}</span>
        ${form}
      </div>`;
  });
  return `<div class="lm-table" data-endpoint="${attr(endpointId)}">${head}${rows.join('')}</div>`;
}

export function renderPullsHtml(pulls, { isAdmin = false } = {}) {
  const list = (pulls || []).filter(Boolean);
  if (!list.length) return '';
  const rows = list.map(p => {
    const pct = Number(p.percent) || 0;
    const active = !!p.active;
    const state = p.status || '';
    const label = active
      ? `${esc(p.status_text || 'pulling')}${p.total ? ` · ${esc(fmtGb(p.completed))} / ${esc(fmtGb(p.total))}` : ''}`
      : state === 'done' ? 'done' : state === 'cancelled' ? 'cancelled'
        : state === 'lost' ? 'lost — the server restarted; pull it again to resume'
          : `failed: ${esc(p.error || 'unknown error')}`;
    const cancel = active && isAdmin
      ? `<button type="button" class="admin-btn-sm" data-lm-action="cancel-pull" data-lm-pull="${attr(p.id)}">Cancel</button>`
      : (!active ? `<button type="button" class="admin-btn-sm" data-lm-action="dismiss-pull" data-lm-pull="${attr(p.id)}" title="Hide">×</button>` : '');
    return `
      <div class="lm-pull lm-pull-${attr(state)}" data-lm-pull-row="${attr(p.id)}">
        <div class="lm-pull-head">
          <span class="lm-name">${esc(p.name)}</span>
          <span class="lm-muted lm-pull-label">${label}</span>
          ${cancel}
        </div>
        <div class="lm-pull-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct.toFixed(0)}">
          <span class="lm-pull-fill${active && !p.total ? ' lm-pull-indeterminate' : ''}" style="width:${(state === 'done' ? 100 : pct).toFixed(1)}%"></span>
        </div>
      </div>`;
  });
  return `<div class="lm-pulls">${rows.join('')}</div>`;
}

export function renderDiscoverHtml(items, { isAdmin = false, q = '' } = {}) {
  const list = items || [];
  if (!list.length) {
    return `<div class="admin-empty">Nothing in the catalogue matches “${esc(q)}”. You can still type its exact name above and pull it.</div>`;
  }
  const cards = list.map(entry => {
    const tags = (entry.tags || []).map(t => {
      const installed = !!t.installed;
      const name = t.name || `${entry.name}:${t.tag}`;
      const pull = isAdmin && !installed
        ? `<button type="button" class="admin-btn-sm lm-tag-pull" data-lm-action="pull" data-lm-name="${attr(name)}" title="ollama pull ${attr(name)}">Pull</button>`
        : '';
      return `
        <span class="lm-tag${installed ? ' lm-tag-installed' : ''}${t.tag === entry.default_tag ? ' lm-tag-default' : ''}" data-lm-tag="${attr(name)}">
          <span class="lm-tag-name" title="${attr(name)}">${esc(t.tag)}</span>
          <span class="lm-muted">${esc(t.params || '')}</span>
          ${fitBadgeHtml(t.fit, t.size_bytes)}
          ${installed ? '<span class="lm-pill lm-pill-installed">installed</span>' : pull}
        </span>`;
    });
    return `
      <div class="lm-disc" data-lm-disc="${attr(entry.name)}">
        <div class="lm-disc-head">
          <span class="lm-name">${esc(entry.name)}</span>
          <span class="lm-muted">${esc(entry.vendor || '')}</span>
          ${capsHtml(entry.capabilities)}
        </div>
        <div class="lm-disc-blurb">${esc(entry.blurb || '')}</div>
        <div class="lm-tags">${tags.join('')}</div>
      </div>`;
  });
  return `<div class="lm-discover-list">${cards.join('')}</div>`;
}

/** The line under the Discover heading: what the fit badges were judged
 *  against. Plain text (set through textContent). */
export function discoverNoteText(vram) {
  if (!vram || !vram.supported) {
    return 'Sizes are approximate (the default build of each tag). No VRAM reading, so no fit verdict.';
  }
  const count = Number(vram.count) || 0;
  const against = `${vram.name || 'your card'}${count > 1 ? ` (${count} GPUs)` : ''}`;
  return `Sizes are approximate (the default build of each tag). Fit is against ${against} with nothing loaded: ${fmtGb(vram.clean_budget_bytes)} usable of ${fmtGb(vram.total_bytes)}.`;
}

export function renderEndpointOptionsHtml(endpoints, selected) {
  return (endpoints || []).map(ep => {
    const where = ep.same_machine ? 'this machine' : 'remote';
    return `<option value="${attr(ep.id)}"${ep.id === selected ? ' selected' : ''}>${esc(ep.name)} — ${esc(where)}</option>`;
  }).join('');
}

// ── state + DOM ─────────────────────────────────────────────────────────────

let _bound = false;
let _active = false;
let _timer = null;
let _loading = false;
let _data = null;
let _endpointId = '';
let _optionsOpen = '';
let _installedFormFor = '';   // the model whose options form the painted table holds
let _discoverQ = '';
let _discoverTimer = null;
let _discoverSeq = 0;
const _pulls = new Map();      // id → last snapshot
const _sources = new Map();    // id → EventSource
const _dismissed = new Set();

function el(id) { return document.getElementById(id); }
function isAdmin() { return !!window._isAdmin; }
function toast(msg, ms = 2200) { try { uiModule.showToast(msg, ms); } catch (_) {} }

async function _confirm(message, confirmText) {
  if (uiModule && typeof uiModule.styledConfirm === 'function') {
    return uiModule.styledConfirm(message, { confirmText, danger: true, title: 'Local models' });
  }
  // No native dialogs: without the app's dialog we refuse rather than block the page.
  return false;
}

async function _json(url, opts = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...opts });
  let body = null;
  try { body = await res.json(); } catch (_) { body = null; }
  if (!res.ok) {
    const detail = body && (body.detail || body.error);
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${res.status}`);
  }
  return body;
}

/** The cards of the endpoint (`[{index, name, total_bytes}]`): the route's
 *  top-level `gpus[]`, else the ones inside the vram block. */
function _cards(data) {
  if (!data) return [];
  if (Array.isArray(data.gpus)) return data.gpus;
  if (data.vram && Array.isArray(data.vram.gpus)) return data.vram.gpus;
  return [];
}

function _setStatus(text, isError = false) {
  const box = el('lm-status');
  if (!box) return;
  box.textContent = text || '';
  box.classList.toggle('is-error', !!isError && !!text);
  box.hidden = !text;
}

function _renderAll() {
  if (!_data) return;
  const admin = isAdmin();
  const sel = el('lm-endpoint');
  if (sel) {
    sel.innerHTML = renderEndpointOptionsHtml(_data.endpoints, _endpointId);
    sel.hidden = !(_data.endpoints && _data.endpoints.length > 1);
  }
  const ep = (_data.endpoints || []).find(e => e.id === _endpointId) || {};
  const gpus = _cards(_data);
  const vram = el('lm-vram');
  if (vram) vram.innerHTML = renderVramHtml(_data.vram, _data.loaded);
  const loaded = el('lm-loaded');
  if (loaded) loaded.innerHTML = renderLoadedHtml(_data.loaded, { isAdmin: admin, endpointId: _endpointId, gpus });
  const installed = el('lm-installed');
  // The 8 s poll must not repaint the table while an options form is open:
  // the form is rendered from the saved values, so whatever the admin had
  // typed was wiped before Save (seen live). The table repaints again as
  // soon as the form closes, saves, or moves to another model.
  if (installed && !(_optionsOpen && _installedFormFor === _optionsOpen)) {
    installed.innerHTML = renderInstalledHtml(_data.models, {
      isAdmin: admin,
      endpointId: _endpointId,
      canSetDefault: !!ep.id && ep.id !== 'ollama-local',
      optionsOpen: _optionsOpen,
      gpus,
    });
    _installedFormFor = _optionsOpen;
  }
  const count = el('lm-installed-count');
  if (count) {
    const n = (_data.models || []).length;
    count.textContent = n ? `${n} model${n === 1 ? '' : 's'}` : '';
  }
  if (_data.reachable === false) {
    _setStatus(_data.error || 'Ollama is unreachable.', true);
  } else {
    _setStatus('');
  }
  const pullBtn = el('lm-pull-btn');
  if (pullBtn) pullBtn.disabled = !admin;
  const pullInput = el('lm-pull-name');
  if (pullInput) pullInput.disabled = !admin;
  const hint = el('lm-admin-hint');
  if (hint) hint.hidden = admin;
  _renderPulls();
}

function _renderPulls() {
  const box = el('lm-pulls');
  if (!box) return;
  const list = Array.from(_pulls.values())
    .filter(p => !_dismissed.has(p.id))
    .sort((a, b) => (Number(!!b.active) - Number(!!a.active)) || ((b.started_at || 0) - (a.started_at || 0)));
  box.innerHTML = renderPullsHtml(list, { isAdmin: isAdmin() });
  box.hidden = !list.length;
}

async function refresh({ silent = false } = {}) {
  if (_loading) return;
  _loading = true;
  // The active pulls this page knew BEFORE asking: a pull started while
  // the poll is in flight is not in the poll's answer and is no ghost.
  const knownActive = Array.from(_pulls.values()).filter(p => p.active).map(p => p.id);
  try {
    const url = `${API}${_endpointId ? `?endpoint_id=${encodeURIComponent(_endpointId)}` : ''}`;
    const data = await _json(url);
    _data = data;
    if (data.endpoint_id) _endpointId = data.endpoint_id;
    const listed = new Set();
    (data.pulls || []).forEach(p => {
      listed.add(p.id);
      // The SSE stream may already know a newer state than this poll (the
      // `version` counter is the server's own); never step backwards.
      const cur = _pulls.get(p.id);
      if (cur && Number(cur.version || 0) > Number(p.version || 0)) return;
      _pulls.set(p.id, p);
      if (p.active) _attach(p.id);
    });
    // Pulls live in the server's memory: an active pull the server no
    // longer lists (for this endpoint) is gone — it restarted. Without
    // this the row stayed "pulling 50 %" for ever with a Cancel that 404s,
    // while the EventSource reconnected into a 404 on every retry.
    knownActive.forEach(id => {
      const p = _pulls.get(id);
      if (!p || !p.active || listed.has(id)) return;
      if (p.endpoint_id && data.endpoint_id && p.endpoint_id !== data.endpoint_id) return;
      _pulls.set(id, {
        ...p, active: false, status: 'lost', status_text: 'lost',
        error: 'lost — the server restarted', finished_at: Date.now() / 1000,
        version: Number(p.version || 0) + 1,
      });
      const es = _sources.get(id);
      if (es) { try { es.close(); } catch (_) {} _sources.delete(id); }
    });
    _renderAll();
  } catch (e) {
    if (!silent) _setStatus(`Could not load local models: ${e.message || e}`, true);
  } finally {
    _loading = false;
  }
}

function _attach(id) {
  if (_sources.has(id) || typeof EventSource === 'undefined') return;
  let es;
  try { es = new EventSource(`${API}/pulls/${encodeURIComponent(id)}/events`); } catch (_) { return; }
  _sources.set(id, es);
  es.onmessage = ev => {
    if (!ev.data || ev.data === '{}') return;
    let snap = null;
    try { snap = JSON.parse(ev.data); } catch (_) { return; }
    if (!snap || !snap.id) return;
    _pulls.set(snap.id, snap);
    _renderPulls();
  };
  const done = () => {
    try { es.close(); } catch (_) {}
    _sources.delete(id);
    const snap = _pulls.get(id);
    if (snap && snap.status === 'done') {
      toast(`Pulled ${snap.name}`);
      _afterModelsChanged();
    } else if (snap && snap.status === 'error') {
      toast(`Pull failed: ${snap.error || snap.name}`, 4000);
    }
  };
  es.addEventListener('end', done);
  es.onerror = () => {
    // A finished job closes the stream; a dropped connection is retried by
    // the browser. Only treat it as final when the job says so.
    const snap = _pulls.get(id);
    if (snap && !snap.active) done();
  };
}

function _detachAll() {
  _sources.forEach(es => { try { es.close(); } catch (_) {} });
  _sources.clear();
}

function _afterModelsChanged() {
  refresh({ silent: true });
  try {
    if (window.modelsModule && typeof window.modelsModule.refreshModels === 'function') {
      window.modelsModule.refreshModels(true).then(() => {
        if (window.sessionModule && window.sessionModule.updateModelPicker) window.sessionModule.updateModelPicker();
      }).catch(() => {});
    }
  } catch (_) {}
}

async function startPull(name) {
  const clean = String(name || '').trim();
  if (!clean) return;
  if (!/^[A-Za-z0-9._/:-]+$/.test(clean)) {
    toast('That does not look like an Ollama model name (letters, digits, . _ - / :)', 3000);
    return;
  }
  try {
    const out = await _json(`${API}/pull?stream=false`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint_id: _endpointId, name: clean }),
    });
    const job = out && out.pull;
    if (job && job.id) {
      _dismissed.delete(job.id);
      _pulls.set(job.id, job);
      _renderPulls();
      _attach(job.id);
      toast(out.created === false ? `Already pulling ${clean}` : `Pulling ${clean}…`);
    }
  } catch (e) {
    toast(`Pull failed: ${e.message || e}`, 4000);
  }
}

async function _post(path, body) {
  return _json(`${API}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ endpoint_id: _endpointId, ...body }),
  });
}

async function _action(action, btn) {
  const name = btn.getAttribute('data-lm-name') || '';
  const pullId = btn.getAttribute('data-lm-pull') || '';
  try {
    switch (action) {
      case 'load': {
        btn.disabled = true;
        btn.textContent = 'Loading…';
        await _post('/load', { name, embedding: btn.getAttribute('data-lm-embedding') === '1' });
        toast(`Loaded ${name}`);
        await refresh({ silent: true });
        break;
      }
      case 'unload': {
        btn.disabled = true;
        await _post('/unload', { name, embedding: btn.getAttribute('data-lm-embedding') === '1' });
        toast(`Unloaded ${name}`);
        await refresh({ silent: true });
        break;
      }
      case 'delete': {
        const ok = await _confirm(`Delete ${name} from this Ollama? The files are removed from disk; pull it again to get it back.`, 'Delete');
        if (!ok) return;
        btn.disabled = true;
        await _json(`${API}/${name.split('/').map(encodeURIComponent).join('/')}?endpoint_id=${encodeURIComponent(_endpointId)}`, { method: 'DELETE' });
        toast(`Deleted ${name}`);
        _afterModelsChanged();
        break;
      }
      case 'default': {
        const res = await fetch('/api/auth/settings', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ default_endpoint_id: _endpointId, default_model: name }),
        });
        invalidateSettings();
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        toast(`${name} is now the default chat model`);
        break;
      }
      case 'options': {
        _optionsOpen = _optionsOpen === name ? '' : name;
        _renderAll();
        if (_optionsOpen) {
          const input = document.querySelector(`[data-lm-options-form="${CSS.escape(name)}"] input[name="num_ctx"]`);
          if (input && typeof input.focus === 'function') input.focus();
        }
        break;
      }
      case 'close-options': {
        _optionsOpen = '';
        _renderAll();
        break;
      }
      case 'save-options': {
        const form = btn.closest('form');
        if (!form) return;
        // Every key is sent, "" clears it on the server (main_gpu "" = Auto).
        const options = {};
        ['num_ctx', 'num_gpu', 'keep_alive', 'main_gpu'].forEach(k => {
          const input = form.querySelector(`[name="${k}"]`);
          options[k] = input ? String(input.value || '').trim() : '';
        });
        btn.disabled = true;
        const out = await _json(`${API}/${name.split('/').map(encodeURIComponent).join('/')}/options?endpoint_id=${encodeURIComponent(_endpointId)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ options }),
        });
        const saved = out && out.options ? out.options : {};
        toast(Object.keys(saved).length ? `Saved options for ${name}` : `Cleared options for ${name}`);
        _optionsOpen = '';
        const row = (_data && _data.models || []).find(m => m.name === name);
        if (row) row.options = saved;
        _renderAll();
        break;
      }
      case 'pull': {
        await startPull(name);
        break;
      }
      case 'cancel-pull': {
        btn.disabled = true;
        await _json(`${API}/pulls/${encodeURIComponent(pullId)}`, { method: 'DELETE' });
        toast('Pull cancelled');
        break;
      }
      case 'dismiss-pull': {
        _dismissed.add(pullId);
        _renderPulls();
        break;
      }
      default:
        break;
    }
  } catch (e) {
    toast(`${action} failed: ${e.message || e}`, 4000);
    if (btn) btn.disabled = false;
    refresh({ silent: true });
  }
}

async function _loadDiscover() {
  const box = el('lm-discover');
  if (!box) return;
  const seq = ++_discoverSeq;
  try {
    const url = `${API}/discover?q=${encodeURIComponent(_discoverQ)}${_endpointId ? `&endpoint_id=${encodeURIComponent(_endpointId)}` : ''}`;
    const data = await _json(url);
    if (seq !== _discoverSeq) return;
    box.innerHTML = renderDiscoverHtml(data.items, { isAdmin: isAdmin(), q: _discoverQ });
    const note = el('lm-discover-note');
    if (note) note.textContent = discoverNoteText(data.vram);
  } catch (e) {
    if (seq !== _discoverSeq) return;
    box.innerHTML = `<div class="admin-empty">Could not load the catalogue: ${esc(e.message || e)}</div>`;
  }
}

function _bind() {
  if (_bound) return;
  const root = document.querySelector('[data-settings-panel="local-models"]');
  if (!root) return;
  _bound = true;

  root.addEventListener('click', ev => {
    const btn = ev.target && ev.target.closest ? ev.target.closest('[data-lm-action]') : null;
    if (!btn || !root.contains(btn)) return;
    const action = btn.getAttribute('data-lm-action');
    if (action === 'save-options') return;   // handled on submit
    ev.preventDefault();
    _action(action, btn);
  });
  root.addEventListener('submit', ev => {
    const form = ev.target;
    if (!form) return;
    if (form.id === 'lm-pull-form') {
      ev.preventDefault();
      const input = el('lm-pull-name');
      const name = input ? input.value : '';
      startPull(name).then(() => { if (input) input.value = ''; });
      return;
    }
    if (form.hasAttribute && form.hasAttribute('data-lm-options-form')) {
      ev.preventDefault();
      const btn = form.querySelector('[data-lm-action="save-options"]');
      if (btn) _action('save-options', btn);
    }
  });

  const sel = el('lm-endpoint');
  if (sel) {
    sel.addEventListener('change', () => {
      _endpointId = sel.value;
      _optionsOpen = '';
      refresh();
      _loadDiscover();
    });
  }
  const refreshBtn = el('lm-refresh');
  if (refreshBtn) refreshBtn.addEventListener('click', () => refresh());

  const q = el('lm-discover-q');
  if (q) {
    q.addEventListener('input', () => {
      _discoverQ = q.value;
      clearTimeout(_discoverTimer);
      _discoverTimer = setTimeout(_loadDiscover, 220);
    });
  }
}

/** Called when the Settings panel becomes visible (settings.js). */
export function activate() {
  _bind();
  if (_active) { refresh({ silent: true }); return; }
  _active = true;
  refresh();
  _loadDiscover();
  clearInterval(_timer);
  _timer = setInterval(() => { if (_active && !document.hidden) refresh({ silent: true }); }, POLL_MS);
}

/** Called when another panel is shown or Settings closes. */
export function deactivate() {
  if (!_active) return;
  _active = false;
  clearInterval(_timer);
  _timer = null;
  // Keep the EventSources of running pulls: the page should still notice
  // when they finish and refresh the picker.
}

export function isActive() { return _active; }

/** Test hook: what the page currently believes about pulls. */
export function _pullsState() { return Array.from(_pulls.values()); }

const localModelsModule = { activate, deactivate, isActive, refresh, startPull };
export default localModelsModule;
