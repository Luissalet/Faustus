// browserView.js — right-hand "Browser" panel: what the agent's browser shows.
//
// The agent loop emits one `browser_view` SSE event after every browser
// ACTION (navigate, click, type, …) carrying a viewport frame plus the page
// URL/title (src/browser_view.py). chat.js hands each event to push(); the
// panel opens itself on the first frame of a turn (unless the user turned
// auto-open off — localStorage `odysseus.browserView.auto`), shows the latest
// frame, a filmstrip of the last 8 frames, and a "Live" dot while the run
// streams (window event `odysseus:chat-busy-change`).
//
// Built like fileViewer.js: same `file-viewer-panel` class family so it sits
// in the same slot of the layout; DOM is created lazily on first use.
//
// XSS: the frame is only ever assigned to img.src after the caller-supplied
// validator (chatRenderer.safeToolScreenshotSrc) accepted it as a raster
// data: URL, and url/title are inserted via textContent / esc(). The URL is
// shown as text, never as a link — clicking it must not navigate the app.

const AUTO_KEY = 'odysseus.browserView.auto';
const MAX_FRAMES = 8;

let _panel = null;
let _frames = [];        // [{src, url, title, tool, at}]
let _active = -1;        // index into _frames shown in the main view
let _live = false;
let _turnHadFrame = false;
let _listening = false;

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** Fallback validator — identical to chatRenderer.safeToolScreenshotSrc; the
 *  chat hook passes the real one so there is a single source of truth. */
function _defaultSafeSrc(raw) {
  const src = String(raw || '').trim();
  return /^data:image\/(?:png|jpe?g|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(src) ? src : '';
}

export function autoOpenEnabled() {
  try {
    const v = localStorage.getItem(AUTO_KEY);
    return v === null ? true : v !== '0';
  } catch (_) { return true; }
}

export function setAutoOpen(on) {
  try { localStorage.setItem(AUTO_KEY, on ? '1' : '0'); } catch (_) {}
  if (_panel) { const cb = _panel.querySelector('.bv-auto-toggle'); if (cb) cb.checked = !!on; }
}

function _ensurePanel() {
  if (_panel && document.body.contains(_panel)) return _panel;
  const el = document.createElement('aside');
  el.id = 'browser-view-panel';
  el.className = 'file-viewer-panel browser-view-panel';
  el.hidden = true;
  el.innerHTML =
    `<div class="fv-head bv-head">` +
    `<div class="fv-title"><span class="fv-kicker">Browser</span><span class="bv-live" title="The agent is driving the browser right now" hidden><span class="bv-live-dot"></span>Live</span></div>` +
    `<div class="fv-actions">` +
    `<label class="bv-auto" title="Open this panel automatically when the agent uses the browser"><input type="checkbox" class="bv-auto-toggle"> Auto-open</label>` +
    `<button type="button" class="fv-btn fv-icon fv-close" data-bv="close" title="Close">×</button>` +
    `</div></div>` +
    `<div class="fv-meta bv-meta"><span class="bv-page-title"></span><span class="bv-url" title=""></span></div>` +
    `<div class="fv-body bv-body">` +
    `<div class="bv-frame"><img class="bv-img" alt="Browser frame" hidden><div class="bv-empty">No frame yet.</div></div>` +
    `<div class="bv-filmstrip" role="list"></div>` +
    `</div>`;
  document.body.appendChild(el);
  const cb = el.querySelector('.bv-auto-toggle');
  if (cb) {
    cb.checked = autoOpenEnabled();
    cb.addEventListener('change', () => setAutoOpen(cb.checked));
  }
  el.addEventListener('click', (e) => {
    const b = e.target.closest('[data-bv]');
    if (!b) return;
    const a = b.dataset.bv;
    if (a === 'close') close();
    else if (a === 'frame') { const i = Number(b.dataset.index); if (Number.isInteger(i)) show(i); }
  });
  document.addEventListener('keydown', (e) => {
    if (el.hidden || e.key !== 'Escape') return;
    // fileViewer owns Escape while it is open (it closes itself)
    if (window.fileViewer && window.fileViewer.isOpen && window.fileViewer.isOpen()) return;
    close();
  });
  _panel = el;
  _listen();
  return el;
}

function _listen() {
  if (_listening || typeof window === 'undefined' || !window.addEventListener) return;
  _listening = true;
  window.addEventListener('odysseus:chat-busy-change', (e) => {
    const active = !!(e && e.detail && e.detail.active);
    setLive(active);
    if (active) _turnHadFrame = false;   // next frame is "the first of the turn"
  });
}

/** Filmstrip markup for the last frames; exported for tests. */
export function renderFilmstripHtml(frames, active) {
  return frames.map((f, i) =>
    `<button type="button" class="bv-thumb${i === active ? ' active' : ''}" data-bv="frame" data-index="${i}" role="listitem" title="${esc(f.title || f.url || '')}">` +
    `<img src="${esc(f.src)}" alt="${esc(f.title || 'frame ' + (i + 1))}">` +
    `<span class="bv-thumb-n">${i + 1}</span></button>`
  ).join('');
}

function _render() {
  const el = _ensurePanel();
  const f = _active >= 0 ? _frames[_active] : null;
  const img = el.querySelector('.bv-img');
  const empty = el.querySelector('.bv-empty');
  const title = el.querySelector('.bv-page-title');
  const url = el.querySelector('.bv-url');
  if (f) {
    img.src = f.src;              // validated in push()
    img.hidden = false;
    if (empty) empty.hidden = true;
    title.textContent = f.title || '';
    url.textContent = f.url || '';
    url.setAttribute('title', f.url || '');
  } else {
    img.hidden = true;
    if (empty) empty.hidden = false;
    title.textContent = '';
    url.textContent = '';
  }
  el.querySelector('.bv-filmstrip').innerHTML = renderFilmstripHtml(_frames, _active);
  const live = el.querySelector('.bv-live');
  if (live) live.hidden = !_live;
}

/** Add a frame from a `browser_view` event. Returns the stored frame, or
 *  null when the screenshot is not an acceptable raster data URL.
 *  `safeSrc` is chatRenderer.safeToolScreenshotSrc (single source of truth). */
export function push(ev, safeSrc) {
  if (!ev || typeof ev !== 'object') return null;
  const validate = typeof safeSrc === 'function' ? safeSrc : _defaultSafeSrc;
  const src = validate(ev.screenshot);
  if (!src) return null;
  const frame = {
    src,
    url: String(ev.url || '').slice(0, 2048),
    title: String(ev.title || '').slice(0, 300),
    tool: String(ev.tool || ''),
    at: Date.now(),
  };
  _frames.push(frame);
  if (_frames.length > MAX_FRAMES) _frames.splice(0, _frames.length - MAX_FRAMES);
  _active = _frames.length - 1;
  const first = !_turnHadFrame;
  _turnHadFrame = true;
  _ensurePanel();
  if (first && autoOpenEnabled()) open();
  _render();
  return frame;
}

export function show(index) {
  if (!Number.isInteger(index) || index < 0 || index >= _frames.length) return;
  _active = index;
  _render();
}

export function setLive(on) {
  _live = !!on;
  if (_panel) { const live = _panel.querySelector('.bv-live'); if (live) live.hidden = !_live; }
}

export function open() {
  const el = _ensurePanel();
  el.hidden = false;
  document.body.classList.add('browser-view-open');
  _render();
}

export function close() {
  if (_panel) _panel.hidden = true;
  document.body.classList.remove('browser-view-open');
}

export function isOpen() { return !!(_panel && !_panel.hidden); }

export function frames() { return _frames.slice(); }

export function reset() {
  _frames = []; _active = -1; _turnHadFrame = false; _live = false;
  if (_panel) _render();
}

const browserView = { push, show, open, close, isOpen, setLive, frames, reset, autoOpenEnabled, setAutoOpen, renderFilmstripHtml, esc };
if (typeof window !== 'undefined') window.browserView = browserView;
export default browserView;
