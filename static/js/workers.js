// static/js/workers.js — the Workers page: hand a task to the local workers in
// plain language, without going through a chat's coordinator model.
//
// One box (what to do), the folder they may touch, Run. Each job runs the
// same machinery as /agents in a chat (POST /api/dispatch, src/dispatch.py):
// the control board, steer/stop and the transcripts live in a "Workers" chat
// the list links to. The list shows every job with its status and, when
// done, the compact result (what changed, tests, the worker's last words).
//
// Same page an outside coordinator (Fable through the faustus-workers MCP
// server) uses — the jobs it starts appear here too.

let _open = false;
let _escHandler = null;
let _pollTimer = null;
let _jobs = [];
let _expanded = new Set();
let _lastWorkspace = '';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function attr(s) { return esc(s); }
function fmtGb(b) { const n = Number(b) || 0; return n >= 1073741824 ? `${(n / 1073741824).toFixed(1)} GB` : `${Math.round(n / 1048576)} MB`; }
function fmtDur(s) { const n = Math.round(Number(s) || 0); return n < 90 ? `${n} s` : n < 3600 ? `${Math.round(n / 60)} min` : `${(n / 3600).toFixed(1)} h`; }
function when(ts) { if (!ts) return ''; try { return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); } catch (_) { return ''; } }

const STATUS_WORD = { queued: 'queued', running: 'running', verifying: 'verifying', done: 'done', partial: 'partial', error: 'error',
  cancelling: 'cancelling', cancelled: 'cancelled', interrupted: 'interrupted' };
const LIVE = new Set(['queued', 'running', 'verifying', 'cancelling']);
export function isLive(status) { return LIVE.has(status); }

function verificationHtml(v) {
  if (!v) return '';
  if (!v.ran) return `<div class="wk-verify wk-verify-none">Not verified — ${esc(v.summary || '')}</div>`;
  const state = v.ok ? 'passed' : (v.inconclusive ? 'inconclusive' : 'failed');
  const fails = Array.isArray(v.failures) ? v.failures : [];
  const pre = new Set(Array.isArray(v.pre_existing) ? v.pre_existing : []);
  return `<div class="wk-verify wk-verify-${state}"><b>Verification ${esc(state)}</b> — ${esc(v.summary || '')}` +
    `${v.command ? ` <code>${esc(v.command)}</code>` : ''}${v.attempts > 1 ? ` · ${esc(v.attempts)} attempts` : ''}` +
    (fails.length ? `<ul class="wk-fails">${fails.map(f => `<li>${esc(f)}${pre.has(f) ? ' <span class="wk-muted">(failed before the job too)</span>' : ''}</li>`).join('')}</ul>` : '') +
    (!v.ok && v.output_tail ? `<details class="wk-tail"><summary>output</summary><pre>${esc(String(v.output_tail).slice(-1500))}</pre></details>` : '') +
    (Array.isArray(v.previous) && v.previous.length ? `<div class="wk-muted">Before the fix round${v.previous.length > 1 ? 's' : ''}: ${v.previous.map(p => esc((p.summary || '') + ((p.failures || []).length ? ' — ' + p.failures.slice(0, 3).join('; ') : ''))).join(' · ')}</div>` : '') +
    `</div>`;
}

function changesHtml(ch, claimedOnly) {
  if (!ch) return '';
  const kinds = [['added', 'added'], ['modified', 'modified'], ['deleted', 'deleted']];
  const parts = kinds.filter(([k]) => Array.isArray(ch[k]) && ch[k].length)
    .map(([k, label]) => `<span class="wk-chg-${k}">${label}:</span> ${ch[k].map(f => `<code>${esc(f)}</code>`).join(' ')}`);
  const claimed = Array.isArray(claimedOnly) && claimedOnly.length
    ? `<div class="wk-claimed">Claimed by a worker but not changed: ${claimedOnly.map(f => `<code>${esc(f)}</code>`).join(' ')}</div>` : '';
  return `<div class="wk-changes"><b>Changed on disk</b> <span class="wk-muted">(${esc(ch.source || '')}${ch.truncated ? ', list truncated' : ''})</span>: ` +
    (parts.length ? parts.join(' · ') : 'nothing') + `</div>${claimed}`;
}

/** One job row + (expanded) its compact result. Exported for tests. */
export function jobHtml(job, expanded = false) {
  const st = STATUS_WORD[job.status] || job.status || '';
  const live = isLive(job.status);
  const res = job.result || {};
  const workers = Array.isArray(res.workers) ? res.workers : [];
  const v = res.verification;
  // a list row has no result: read the count and the verification word off the verdict line
  const verdict = String(job.verdict || '');
  const vm = verdict.match(/(\d+) files? changed on disk/);
  const changed = Array.isArray(res.files_changed) && job.result ? res.files_changed : (vm ? new Array(parseInt(vm[1], 10)).fill('') : []);
  const vok = v && v.ran ? !!v.ok : /verification passed/.test(verdict);
  const vword = v && v.ran ? (v.ok ? 'verified' : (v.inconclusive ? 'unverified' : 'verification failed'))
    : (/verification passed/.test(verdict) ? 'verified' : (/verification FAILED/.test(verdict) ? 'verification failed' : ''));
  const head =
    `<div class="wk-job-head" data-wk-toggle="${attr(job.id)}">` +
    `<span class="wk-status wk-status-${attr(st)}">${esc(st)}</span>` +
    `<span class="wk-title" title="${attr(job.verdict || job.title || '')}">${esc(job.title || 'Workers')}</span>` +
    `<span class="wk-meta">${esc(when(job.created))}${job.duration_s != null ? ' · ' + esc(fmtDur(job.duration_s)) : ''}` +
    `${changed.length ? ` · ${changed.length} file${changed.length > 1 ? 's' : ''} changed` : ''}` +
    `${vword ? ` · <span class="wk-vword wk-vword-${vok ? 'ok' : 'bad'}">${esc(vword)}</span>` : ''}` +
    `${res.totals && res.totals.errors ? ` · ${res.totals.errors} error${res.totals.errors > 1 ? 's' : ''}` : ''}</span>` +
    `<span class="wk-actions">` +
    (job.session_id ? `<button type="button" class="admin-btn-sm" data-wk-open="${attr(job.session_id)}" title="Open the Workers chat: the control board, steer / stop, the transcripts">Board</button>` : '') +
    (live ? `<button type="button" class="admin-btn-sm" data-wk-cancel="${attr(job.id)}">Cancel</button>` : '') +
    `</span></div>`;
  if (!expanded) return `<div class="wk-job" data-wk-job="${attr(job.id)}">${head}</div>`;
  const rows = [];
  if (job.error) rows.push(`<div class="wk-error">${esc(job.error)}</div>`);
  if (job.verdict && !live) rows.push(`<div class="wk-verdict">${esc(job.verdict)}</div>`);
  if (live) {
    if (job.phase) rows.push(`<div class="wk-muted wk-phase">${esc(job.phase)}${job.ceiling_s ? ` · at most ${esc(fmtDur(job.ceiling_s))} more` : ''}</div>`);
    const prog = job.progress || {};
    const names = Object.keys(prog);
    rows.push(`<div class="wk-progress">${names.length ? names.map(n => {
      const p = prog[n] || {};
      const bits = [esc(p.last_event || '…')];
      if (p.round != null) bits.push(`round ${esc(p.round)}`);
      if (p.last_tool || p.tool) bits.push(esc(p.last_tool || p.tool));
      if (p.elapsed_s != null) bits.push(`${esc(Math.round(p.elapsed_s))} s`);
      if (p.stalled) bits.push(`<b>stalled</b>${p.stall_reason ? ' (' + esc(p.stall_reason) + ')' : ''}`);
      return `<div class="wk-worker-line"><span class="wk-wname">${esc(n)}</span> ${bits.join(' · ')}</div>`;
    }).join('') : '<span class="wk-muted">starting…</span>'}</div>`);
  }
  if (!live) {
    rows.push(changesHtml(res.changes, res.claimed_only));
    rows.push(verificationHtml(v));
  }
  for (const w of workers) {
    const files = Array.isArray(w.files_changed) ? w.files_changed : [];
    const wst = w.status || '';
    rows.push(`<div class="wk-worker">` +
      `<div class="wk-worker-line"><span class="wk-status wk-status-${attr(wst)}">${esc(wst)}</span>` +
      `<span class="wk-wname">${esc(w.name || 'worker')}${w.role && w.role !== 'worker' ? ` <span class="wk-muted">(${esc(w.role)})</span>` : ''}</span>` +
      `<span class="wk-muted">${esc(w.rounds || 0)} rounds · ${esc(w.tool_calls || 0)} tools${w.failed_calls ? ' (' + esc(w.failed_calls) + ' failed)' : ''} · ${esc(w.input_tokens || 0)}/${esc(w.output_tokens || 0)} tok${w.stop_reason && w.stop_reason !== 'complete' ? ' · ' + esc(w.stop_reason) : ''}</span></div>` +
      (w.error ? `<div class="wk-error">${esc(w.error)}</div>` : '') +
      (files.length ? `<div class="wk-files">claims: ${files.map(f => `<code>${esc(f)}</code>`).join(' ')}</div>` : '') +
      (w.summary ? `<div class="wk-summary">${esc(w.summary)}</div>` : '') +
      `</div>`);
  }
  if (Array.isArray(res.lock_conflicts) && res.lock_conflicts.length) rows.push(`<div class="wk-muted">Writes refused by the file locks: ${esc(res.lock_conflicts.join('; '))}</div>`);
  if (res.dropped_tasks) rows.push(`<div class="wk-error">${esc(res.dropped_tasks)} task(s) were not run (max 4 per job) — run them again.</div>`);
  const tasks = Array.isArray(job.tasks) ? job.tasks : [];
  rows.push(`<details class="wk-tasks"><summary>${tasks.length} task${tasks.length === 1 ? '' : 's'} · ${esc(job.workspace || 'no workspace')} · ${esc(job.model || '')}` +
    `${job.verify && job.verify !== 'auto' ? ` · verify: ${esc(job.verify)}` : ''}</summary>` +
    tasks.map((t, i) => `<div class="wk-task"><b>${i + 1}.</b> ${esc(t.instruction || '')}${t.files && t.files.length ? ` <span class="wk-muted">[${esc(t.files.join(', '))}]</span>` : ''}</div>`).join('') + `</details>`);
  return `<div class="wk-job wk-job-open" data-wk-job="${attr(job.id)}">${head}<div class="wk-job-body">${rows.join('')}</div></div>`;
}

/** The whole modal body. Exported for tests. */
export function pageHtml(jobs, expanded, { workspace = '', busy = false } = {}) {
  const list = jobs.length ? jobs.map(j => jobHtml(j, expanded.has(j.id))).join('')
    : '<div class="admin-empty">No jobs yet. Describe a task above and press Run — the workers do it on the local models; Faustus checks what changed and runs the tests itself; you read the verdict.</div>';
  return `
    <form class="wk-form" id="wk-form">
      <textarea id="wk-task" rows="4" placeholder="What should the workers do? Say what 'done' means, e.g. “In cart.py add apply_discount(total, pct) with validation and a test in tests/test_cart.py; pytest -q must pass.” Separate several tasks with a blank line or a list (- / 1.) — one worker each." ${busy ? 'disabled' : ''}></textarea>
      <div class="wk-form-row">
        <label class="wk-field">Folder <input type="text" id="wk-workspace" placeholder="D:\\projects\\app" value="${attr(workspace)}" required ${busy ? 'disabled' : ''}></label>
        <label class="wk-check" title="Independent tasks run at the same time (one worker each); off = one after another (a later task may edit what an earlier one wrote)"><input type="checkbox" id="wk-parallel" checked> parallel</label>
        <label class="wk-check" title="Add a reviewer worker after the others"><input type="checkbox" id="wk-reviewer"> reviewer</label>
        <label class="wk-field wk-field-sm">Model <input type="text" id="wk-model" placeholder="configured worker model"></label>
        <button type="submit" class="admin-btn-add" id="wk-run" ${busy ? 'disabled' : ''}>${busy ? 'Starting…' : 'Run'}</button>
      </div>
      <div class="wk-form-row">
        <label class="wk-field" title="Run by Faustus in the folder after the workers — their own claims are never the proof. Empty = the project's test runner is detected (pytest, npm test, cargo, go, make test)">Verify with <input type="text" id="wk-verify" placeholder="auto-detect the test runner"></label>
        <label class="wk-field wk-field-sm" title="When the verification fails: how many times one fixer worker gets the failure output before Faustus gives up">Fix rounds <input type="number" id="wk-fix" min="0" max="2" value="1"></label>
        <span class="wk-muted" id="wk-count">1 worker</span>
      </div>
      <div class="wk-hint">A blank line or a list marker starts a new task = one worker (max 4). The workers are confined to the folder; Faustus checkpoints it before, diffs it after, runs the verification itself and marks the job <em>partial</em> when anything did not finish. The job gets its own <em>Workers</em> chat with the control board. Same door Fable uses from Cowork — see <code>website/fable-workers.md</code>.</div>
    </form>
    <div class="wk-list" id="wk-list">${list}</div>`;
}

/** Split the box into tasks: a blank line or a list marker (-, *, •, 1., 2))
 *  starts a new one; a soft-wrapped paragraph stays ONE task (one line per
 *  task used to turn a wrapped sentence into three workers). Max 4. */
export function parseTasks(text) {
  const marker = /^\s*(?:[-*•]|\d+[.)])\s+/;
  const tasks = [];
  let cur = null;
  for (const raw of String(text || '').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) { if (cur) { tasks.push(cur); cur = null; } continue; }
    if (marker.test(raw)) {
      if (cur) tasks.push(cur);
      cur = line.replace(marker, '').trim();
    } else {
      cur = cur ? `${cur} ${line}` : line;
    }
  }
  if (cur) tasks.push(cur);
  return tasks.filter(Boolean).slice(0, 4);
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

function _toast(msg, ms = 2500) {
  try { if (window.uiModule && window.uiModule.showToast) { window.uiModule.showToast(msg, ms); return; } } catch (_) {}
  try { console.info('[workers]', msg); } catch (_) {}
}

async function _refreshJobs() {
  try {
    const data = await _json('/api/dispatch?limit=50');
    const jobs = Array.isArray(data.jobs) ? data.jobs : [];
    // rows come without results; fetch the compact result for the expanded and the live ones
    const want = jobs.filter(j => _expanded.has(j.id) || isLive(j.status));
    for (const id of Array.from(_expanded)) if (!jobs.some(j => j.id === id)) _expanded.delete(id);
    const full = await Promise.all(want.map(j => _json(`/api/dispatch/${encodeURIComponent(j.id)}`).catch(() => j)));
    const byId = new Map(full.map(j => [j.id, j]));
    _jobs = jobs.map(j => byId.get(j.id) || j);
  } catch (e) {
    _toast(`Workers: ${e.message || e}`, 4000);
  }
  _renderList();
}

function _renderList() {
  const list = document.getElementById('wk-list');
  if (!list) return;
  list.innerHTML = _jobs.length ? _jobs.map(j => jobHtml(j, _expanded.has(j.id))).join('')
    : '<div class="admin-empty">No jobs yet. Describe a task above and press Run.</div>';
  const live = _jobs.some(j => isLive(j.status));
  if (live && !_pollTimer) _pollTimer = setInterval(_refreshJobs, 3000);
  if (!live && _pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

async function _run(modal) {
  const box = modal.querySelector('#wk-task');
  const tasks = parseTasks(box.value);
  if (!tasks.length) { box.focus(); return; }
  const wsInput = modal.querySelector('#wk-workspace');
  const workspace = (wsInput.value || '').trim();
  if (!workspace) { _toast('Say which folder the workers may touch', 3000); wsInput.focus(); return; }
  const body = {
    tasks, workspace, parallel: modal.querySelector('#wk-parallel').checked, reviewer: modal.querySelector('#wk-reviewer').checked,
    fix_rounds: Math.max(0, Math.min(2, parseInt(modal.querySelector('#wk-fix').value, 10) || 0)),
  };
  const verify = (modal.querySelector('#wk-verify').value || '').trim();
  if (verify) body.verify = verify;
  const model = (modal.querySelector('#wk-model').value || '').trim();
  if (model) body.model = model;
  const btn = modal.querySelector('#wk-run');
  btn.disabled = true; btn.textContent = 'Starting…';
  try {
    const job = await _json('/api/dispatch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    _expanded.add(job.id);
    box.value = '';
    _lastWorkspace = workspace;
    try { localStorage.setItem('odysseus-workers-folder', workspace); } catch (_) {}
    _toast(`Started ${tasks.length} worker${tasks.length > 1 ? 's' : ''}`);
    await _refreshJobs();
  } catch (e) {
    _toast(`Could not start: ${e.message || e}`, 5000);
  } finally {
    btn.disabled = false; btn.textContent = 'Run';
  }
}

export function openWorkers() {
  if (_open) return;
  _open = true;
  let workspace = '';
  try { workspace = localStorage.getItem('odysseus-workers-folder') || localStorage.getItem('odysseus-workspace') || ''; } catch (_) {}
  _lastWorkspace = workspace;
  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'workers-modal';
  modal.innerHTML = `
    <div class="modal-content workers-modal-content">
      <div class="modal-header">
        <h4 style="position:relative;top:-2px;">🤖 Workers</h4>
        <span class="wk-muted" style="margin-left:8px">local models do the mechanical work; you read what changed</span>
        <span style="flex:1"></span>
        <button class="close-btn" id="workers-close">✖</button>
      </div>
      <div class="modal-body wk-body">${pageHtml([], _expanded, { workspace })}</div>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelector('#workers-close').addEventListener('click', closeWorkers);
  modal.addEventListener('click', (e) => {
    if (e.target === modal) { closeWorkers(); return; }
    const t = e.target.closest ? e.target : null;
    if (!t) return;
    const openBtn = t.closest('[data-wk-open]');
    if (openBtn) {
      const sid = openBtn.getAttribute('data-wk-open');
      closeWorkers();
      try { if (window.sessionModule && window.sessionModule.selectSession) window.sessionModule.selectSession(sid); else location.hash = '#' + sid; } catch (_) { location.hash = '#' + sid; }
      return;
    }
    const cancelBtn = t.closest('[data-wk-cancel]');
    if (cancelBtn) {
      cancelBtn.disabled = true;
      _json(`/api/dispatch/${encodeURIComponent(cancelBtn.getAttribute('data-wk-cancel'))}/cancel`, { method: 'POST' })
        .then(() => _refreshJobs()).catch(err => _toast(`Cancel failed: ${err.message || err}`, 4000));
      return;
    }
    const head = t.closest('[data-wk-toggle]');
    if (head && !t.closest('button')) {
      const id = head.getAttribute('data-wk-toggle');
      if (_expanded.has(id)) _expanded.delete(id); else _expanded.add(id);
      _refreshJobs();
    }
  });
  modal.querySelector('#wk-form').addEventListener('submit', (e) => { e.preventDefault(); _run(modal); });
  modal.querySelector('#wk-task').addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); _run(modal); }
  });
  // "N workers" as you type, so a pasted paragraph never surprises
  modal.querySelector('#wk-task').addEventListener('input', (e) => {
    const n = parseTasks(e.target.value).length;
    const el = modal.querySelector('#wk-count');
    if (el) el.textContent = `${Math.max(1, n)} worker${n === 1 || n === 0 ? '' : 's'}${n > 1 ? ' (one per task)' : ''}`;
  });
  // the verifier Faustus would run in that folder, shown as the placeholder
  const wsInput = modal.querySelector('#wk-workspace');
  let verifierTimer = null;
  const showVerifier = () => {
    const ws = (wsInput.value || '').trim();
    const inp = modal.querySelector('#wk-verify');
    if (!inp) return;
    if (!ws) { inp.placeholder = 'auto-detect the test runner'; return; }
    _json(`/api/dispatch/config?workspace=${encodeURIComponent(ws)}`).then(cfg => {
      const v = cfg && cfg.verifier;
      if (!v) return;
      inp.placeholder = v.error ? v.error : (v.label ? `auto: ${v.label}` : 'no test runner found here — give a command');
    }).catch(() => {});
  };
  wsInput.addEventListener('input', () => { clearTimeout(verifierTimer); verifierTimer = setTimeout(showVerifier, 500); });
  showVerifier();
  _escHandler = (e) => { if (e.key === 'Escape') closeWorkers(); };
  document.addEventListener('keydown', _escHandler);
  // say which model a job would run on before Run (the configured worker
  // model, else the utility / default chat model — which may be the big one)
  _json('/api/dispatch/config').then(cfg => {
    const inp = modal.querySelector('#wk-model');
    if (!inp) return;
    if (cfg && cfg.model) inp.placeholder = `${cfg.model}${cfg.server ? ' @ ' + cfg.server : ''}`;
    else if (cfg && cfg.error) inp.placeholder = cfg.error;
  }).catch(() => {});
  _refreshJobs();
  setTimeout(() => { const b = modal.querySelector('#wk-task'); if (b) b.focus(); }, 50);
}

export function closeWorkers() {
  if (!_open) return;
  _open = false;
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  if (_escHandler) { document.removeEventListener('keydown', _escHandler); _escHandler = null; }
  const modal = document.getElementById('workers-modal');
  if (modal) modal.remove();
}

export function isWorkersOpen() { return _open; }

const workersModule = { openWorkers, closeWorkers, isWorkersOpen, jobHtml, pageHtml, parseTasks };
if (typeof window !== 'undefined') window.workersModule = workersModule;
export default workersModule;
