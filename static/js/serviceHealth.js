/**
 * serviceHealth.js — the degraded-state readout (FAUSTUS).
 *
 * The backend has had /api/diagnostics/services for a while; nothing in the UI
 * ever called it. That is the whole bug this module fixes: when ChromaDB goes
 * away (Docker closed, container stopped) document RAG and vector memory fall
 * back to keyword matching *silently*, and answers just quietly get worse.
 *
 * What it does:
 *   - polls the report (60s, plus on tab focus) and shows one dot in the
 *     sidebar user bar: green ok, amber degraded, red down;
 *   - toasts once on an ok -> not-ok transition, so a service dying while you
 *     are working actually tells you;
 *   - opens a panel with, per service, what broke and what to do about it
 *     (hints come from the server so they stay testable), and a Reconnect
 *     button that re-establishes the vector stores without restarting.
 *
 * Non-admin sessions get 403 from the endpoint; the module then removes itself
 * rather than retrying forever.
 */

import { showToast } from './ui.js';

const ENDPOINT = '/api/diagnostics/services';
const RECONNECT_ENDPOINT = '/api/diagnostics/services/reconnect';
const POLL_MS = 60000;
const FOCUS_MIN_GAP_MS = 30000;

const LABELS = {
  chromadb: 'Vector stores (ChromaDB)',
  searxng: 'Web search (SearXNG)',
  providers: 'Model endpoints',
  email: 'Email accounts',
  ntfy: 'Push (ntfy)',
};

let _chip = null;
let _panel = null;
let _report = null;
let _timer = null;
let _lastProbe = 0;
let _lastOverall = null;
let _off = false;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function label(name) {
  return LABELS[name] || name;
}

function issues(report) {
  return (report?.services || []).filter(s => s.status === 'degraded' || s.status === 'down');
}

// ── chip ──────────────────────────────────────────────────────────────────

function ensureChip() {
  if (_chip && document.body.contains(_chip)) return _chip;
  const actions = document.querySelector('#sidebar-user-bar .user-bar-actions');
  if (!actions) return null;
  const btn = el('button', 'user-bar-btn svc-health-chip');
  btn.type = 'button';
  btn.id = 'svc-health-chip';
  btn.title = 'Service health';
  btn.setAttribute('aria-label', 'Service health');
  btn.appendChild(el('span', 'svc-health-dot'));
  btn.appendChild(el('span', 'svc-health-count'));
  btn.addEventListener('click', () => openPanel());
  actions.insertBefore(btn, actions.firstChild);
  _chip = btn;
  return btn;
}

function renderChip(report) {
  const chip = ensureChip();
  if (!chip) return;
  const overall = report?.overall || 'ok';
  const bad = issues(report);
  chip.dataset.status = overall;
  chip.querySelector('.svc-health-count').textContent = bad.length ? String(bad.length) : '';
  chip.title = bad.length
    ? `Service health: ${bad.map(s => label(s.name)).join(', ')}`
    : 'Service health: everything reachable';
}

// ── panel ─────────────────────────────────────────────────────────────────

function statusPill(status) {
  const pill = el('span', `svc-pill svc-pill-${status}`, status);
  return pill;
}

function serviceRow(svc) {
  const row = el('div', `svc-row svc-row-${svc.status}`);
  const head = el('div', 'svc-row-head');
  head.appendChild(el('span', 'svc-row-name', label(svc.name)));
  head.appendChild(statusPill(svc.status));
  row.appendChild(head);
  if (svc.detail) row.appendChild(el('div', 'svc-row-detail', svc.detail));
  if (svc.hint?.text) {
    const hint = el('div', 'svc-row-hint');
    hint.appendChild(el('div', 'svc-row-hint-text', svc.hint.text));
    if (svc.hint.command) {
      const line = el('div', 'svc-row-cmd');
      line.appendChild(el('code', 'svc-row-cmd-text', svc.hint.command));
      const copy = el('button', 'svc-copy-btn', 'Copy');
      copy.type = 'button';
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(svc.hint.command);
          copy.textContent = 'Copied';
          setTimeout(() => { copy.textContent = 'Copy'; }, 1500);
        } catch { /* clipboard blocked — the text is selectable anyway */ }
      });
      line.appendChild(copy);
      hint.appendChild(line);
    }
    row.appendChild(hint);
  }
  return row;
}

function renderPanel() {
  if (!_panel) return;
  const body = _panel.querySelector('.svc-panel-body');
  body.textContent = '';
  const services = _report?.services || [];
  if (!services.length) {
    body.appendChild(el('div', 'svc-row-detail', 'No report yet.'));
    return;
  }
  services.forEach(svc => body.appendChild(serviceRow(svc)));
  const stamp = _panel.querySelector('.svc-panel-stamp');
  if (stamp) {
    const when = _report?.timestamp ? new Date(_report.timestamp) : new Date();
    stamp.textContent = `checked ${when.toLocaleTimeString()}`;
  }
}

function closePanel() {
  if (!_panel) return;
  _panel.remove();
  _panel = null;
  document.removeEventListener('keydown', onKeydown, true);
}

function onKeydown(e) {
  if (e.key === 'Escape') { e.stopPropagation(); closePanel(); }
}

function buildPanel() {
  const overlay = el('div', 'svc-health-overlay');
  overlay.id = 'svc-health-overlay';
  const panel = el('div', 'svc-health-panel');
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Service health');

  const head = el('div', 'svc-panel-head');
  head.appendChild(el('h3', 'svc-panel-title', 'Service health'));
  head.appendChild(el('span', 'svc-panel-stamp'));
  const close = el('button', 'svc-panel-close', '×');
  close.type = 'button';
  close.title = 'Close';
  close.addEventListener('click', closePanel);
  head.appendChild(close);
  panel.appendChild(head);

  panel.appendChild(el('div', 'svc-panel-body'));

  const foot = el('div', 'svc-panel-foot');
  const recheck = el('button', 'svc-foot-btn', 'Re-check');
  recheck.type = 'button';
  recheck.addEventListener('click', () => probe(true));
  const recon = el('button', 'svc-foot-btn svc-foot-btn-primary', 'Reconnect vector stores');
  recon.type = 'button';
  recon.addEventListener('click', () => reconnect(recon));
  foot.appendChild(recheck);
  foot.appendChild(recon);
  panel.appendChild(foot);

  overlay.appendChild(panel);
  overlay.addEventListener('mousedown', e => { if (e.target === overlay) closePanel(); });
  document.body.appendChild(overlay);
  document.addEventListener('keydown', onKeydown, true);
  return overlay;
}

export function openPanel() {
  if (_panel) { closePanel(); return; }
  _panel = buildPanel();
  renderPanel();
  probe(true);
}

// ── network ───────────────────────────────────────────────────────────────

async function probe(force) {
  if (_off) return null;
  const now = Date.now();
  if (!force && now - _lastProbe < FOCUS_MIN_GAP_MS) return _report;
  _lastProbe = now;
  try {
    const res = await fetch(ENDPOINT, { credentials: 'same-origin' });
    if (res.status === 401 || res.status === 403) { disable(); return null; }
    if (!res.ok) return null;
    apply(await res.json());
    return _report;
  } catch {
    return null;  // offline / app restarting — the next tick retries
  }
}

async function reconnect(btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Reconnecting…'; }
  try {
    const res = await fetch(RECONNECT_ENDPOINT, { method: 'POST', credentials: 'same-origin' });
    if (res.ok) {
      const report = await res.json();
      apply(report);
      const r = report.recovery || {};
      const healthy = ['rag', 'memory'].filter(k => r[k] === 'healthy').length;
      showToast(healthy
        ? `Vector stores reconnected (${healthy}/2 healthy)`
        : 'Could not reconnect — check the hint in the panel', { duration: 4000 });
    } else if (res.status === 401 || res.status === 403) {
      disable();
    }
  } catch {
    showToast('Reconnect failed — Faustus did not answer', { duration: 4000 });
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Reconnect vector stores'; }
  }
}

function apply(report) {
  _report = report;
  renderChip(report);
  renderPanel();
  const overall = report?.overall || 'ok';
  if (_lastOverall && _lastOverall === 'ok' && overall !== 'ok') {
    const bad = issues(report).map(s => label(s.name)).join(', ');
    showToast(`Service degraded: ${bad}`, {
      duration: 8000, action: 'Details', onAction: () => openPanel(),
    });
  }
  _lastOverall = overall;
}

function disable() {
  _off = true;
  if (_timer) { clearInterval(_timer); _timer = null; }
  if (_chip) { _chip.remove(); _chip = null; }
  closePanel();
}

function start() {
  if (_timer) return;
  probe(true);
  _timer = setInterval(() => probe(true), POLL_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') probe(false);
  });
}

if (typeof window !== 'undefined') {
  window.faustusServiceHealth = { probe, openPanel, get report() { return _report; } };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(start, 2500));
  } else {
    setTimeout(start, 2500);
  }
}

export default { probe, openPanel };
