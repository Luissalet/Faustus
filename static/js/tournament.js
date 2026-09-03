// static/js/tournament.js
// Tournament — the same prompt to several models, blind and in parallel, then
// rounds of explicit fusion over the anonymised answers, then a ranked table.
//
// This sits BESIDE the A/B comparator (static/js/compare/*), which is still the
// place to watch two models answer side by side and vote. A tournament is the
// other question: given N models, what does the best hybrid of their answers
// look like, and which of them got closest to it.
//
// The backend is authoritative (src/tournament.py, routes/tournament_routes.py):
// the scheduling (two entries naming the SAME model serialise behind its one
// slot; different models really do overlap), the anonymisation, the convergence
// stop and the judging all happen there. This module is the human end of it.
//
// Two honesty rules run through the file and are not negotiable:
//
//   * a score the judge did not give renders as an em dash with the reason on
//     it — never as a number, never as a zero; and
//   * a ranking with no judge behind it says so, in words, next to the table.
//
// The renderers are pure and live between the marked region below, so
// tests/test_tournament_page_js.py can run them in bare node.

import uiModule from './ui.js';

const API = `${window.location.origin}/api/tournament`;
const MODELS_API = `${window.location.origin}/api/models`;
const POLL_MS = 1500;

// ── Tournament: pure helpers (dependency-free; extracted and run under node by tests) ──
// Everything between these markers must stay free of DOM, module and window
// references so tests/test_tournament_page_js.py can execute it in bare node.

/** Local escape: same table as ui.js esc(), but import-free for tests. */
function trEsc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

/** Sanitize a server-provided word (state, outcome) for use in a class. */
function trToken(value) {
  return String(value == null ? '' : value).toLowerCase().replace(/[^a-z0-9_-]/g, '');
}

/** A number, or `fallback` when the value is not one. Never NaN. */
function trNum(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

/** An integer, or null when the value cannot be one (never a guess). */
function trInt(value) {
  if (value == null || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

const TRN_AXES = ['correctness', 'completeness', 'sophistication'];
const TRN_LIVE = ['queued', 'running', 'judging', 'cancelling'];
const TRN_MIN_MODELS = 2;
const TRN_MAX_MODELS = 4;

/** Flatten GET /api/models into `[{id, name, endpoint}]`, deduped by id. */
function modelRowsFrom(payload) {
  const items = payload && Array.isArray(payload.items) ? payload.items : [];
  const seen = new Set();
  const out = [];
  for (const item of items) {
    if (!item || typeof item !== 'object') continue;
    const lists = [
      [item.models, item.models_display],
      [item.models_extra, item.models_extra_display],
    ];
    for (const [ids, labels] of lists) {
      if (!Array.isArray(ids)) continue;
      ids.forEach((id, i) => {
        const mid = String(id == null ? '' : id);
        if (!mid || seen.has(mid)) return;
        seen.add(mid);
        const shown = Array.isArray(labels) && labels[i] ? String(labels[i]) : mid;
        out.push({
          id: mid,
          name: shown.split('/').pop(),
          endpoint: String(item.endpoint_name || ''),
        });
      });
    }
  }
  return out;
}

/** One answer row of a result, with every field defaulted. */
function normalizeAnswer(raw) {
  const row = raw && typeof raw === 'object' ? raw : {};
  return {
    entry: trNum(row.entry, 0),
    model: String(row.model == null ? '' : row.model),
    round: trNum(row.round, 0),
    text: String(row.text == null ? '' : row.text),
    elapsed_s: trNum(row.elapsed_s, 0),
    tokens: trNum(row.tokens, 0),
    tokens_source: String(row.tokens_source || 'estimated'),
  };
}

/** One finalist, with the scores left NULL when the judge gave none. */
function normalizeFinal(raw) {
  const row = raw && typeof raw === 'object' ? raw : {};
  const src = row.scores && typeof row.scores === 'object' ? row.scores : null;
  const scores = {};
  for (const axis of TRN_AXES) scores[axis] = src ? trInt(src[axis]) : null;
  return {
    entry: trNum(row.entry, 0),
    model: String(row.model == null ? '' : row.model),
    round: trNum(row.round, 0),
    text: String(row.text == null ? '' : row.text),
    outcome: String(row.outcome || 'success'),
    scores,
    total: trInt(row.total),
    tiebreak: trNum(row.tiebreak, 0),
    note: String(row.note || ''),
    rank: trInt(row.rank),
  };
}

/** The whole run payload, however the server wrapped it. */
function normalizeRun(raw) {
  const src = raw && typeof raw === 'object' ? raw : {};
  const body = src.run && typeof src.run === 'object' ? src.run : src;
  const result = body.result && typeof body.result === 'object' ? body.result : {};
  const finals = Array.isArray(result.final) ? result.final.map(normalizeFinal) : [];
  return {
    id: String(body.id == null ? '' : body.id),
    status: String(body.status || 'queued'),
    error: String(body.error || ''),
    prompt: String(body.prompt == null ? '' : body.prompt),
    models: Array.isArray(body.models) ? body.models.map(m => String(m == null ? '' : m)) : [],
    rounds: trNum(body.rounds, 0),
    judge_model: String(body.judge_model || ''),
    duration_s: trNum(body.duration_s, 0),
    rounds_run: trNum(result.rounds_run, 0),
    stopped_by: String(result.stopped_by || ''),
    convergence: result.convergence && typeof result.convergence === 'object' ? result.convergence : null,
    answers: Array.isArray(result.answers) ? result.answers.map(normalizeAnswer) : [],
    final: finals,
    judge: result.judge && typeof result.judge === 'object' ? result.judge : null,
    ranking: String(result.ranking || ''),
    ranking_note: String(result.ranking_note || ''),
    merge_prompt: String(result.merge_prompt || ''),
    errors: Array.isArray(result.errors) ? result.errors : [],
    cancelled: Array.isArray(result.cancelled) ? result.cancelled : [],
    degraded: Boolean(result.degraded),
    progress: Array.isArray(body.progress) ? body.progress : [],
  };
}

function isLiveStatus(status) {
  return TRN_LIVE.indexOf(String(status || '')) !== -1;
}

/** `entry index → its answers in round order`, for the per-model cards. */
function answersByEntry(run) {
  const out = new Map();
  for (const answer of (run && run.answers) || []) {
    const key = trNum(answer.entry, 0);
    if (!out.has(key)) out.set(key, []);
    out.get(key).push(answer);
  }
  for (const rows of out.values()) rows.sort((a, b) => a.round - b.round);
  return out;
}

/** The winner: rank 1 among the finalists, or null while there are none. */
function winnerOf(run) {
  const finals = (run && run.final) || [];
  for (const row of finals) if (row.rank === 1) return row;
  return finals.length ? finals[0] : null;
}

/** What ended the rounds, in words. Empty while the run is still going. */
function stoppedByLabel(run) {
  const stopped = String((run && run.stopped_by) || '');
  if (stopped === 'convergence') {
    const score = run && run.convergence ? trNum(run.convergence.score, 0) : 0;
    return `stopped early: the rounds converged (${score.toFixed(2)})`;
  }
  if (stopped === 'cancelled') return 'stopped: cancelled';
  if (stopped === 'rounds') {
    const n = trNum(run && run.rounds_run, 0);
    return `ran all ${n} round${n === 1 ? '' : 's'}`;
  }
  return '';
}

/** How the table was ordered — and, when no judge scored it, that it was not. */
function rankingLabel(run) {
  const ranking = String((run && run.ranking) || '');
  if (ranking === 'judge') return { kind: 'judge', text: 'ranked by the judge' };
  const note = String((run && run.ranking_note) || '');
  if (ranking === 'mixed') {
    return { kind: 'mixed', text: note || 'the judge scored only some of the answers' };
  }
  return {
    kind: 'deterministic',
    text: note || 'no judge available — ranked by a deterministic tiebreak',
  };
}

/** A score cell. A score the judge did not give is an em dash, never a zero. */
function scoreCellHtml(value) {
  const n = trInt(value);
  if (n === null) {
    return '<td class="trn-score is-null" title="the judge did not score this">—</td>';
  }
  return `<td class="trn-score">${trEsc(String(n))}</td>`;
}

/** The synthesis prompt for the composer: the server's, or the same thing
 *  rebuilt here so "Merge" still works on a result that predates it. */
function mergePromptFor(run) {
  const ready = String((run && run.merge_prompt) || '');
  if (ready.trim()) return ready;
  const finals = (run && run.final) || [];
  const usable = finals.filter(row => String(row.text || '').trim());
  if (!usable.length) return '';
  const lines = ['Here are the final answers from a model tournament on this task.', '',
    'The task was:', '', String((run && run.prompt) || ''), ''];
  usable.forEach((row, i) => {
    const label = String.fromCharCode(65 + (i % 26));
    const rank = row.rank === null ? '' : ` (ranked ${row.rank}${row.total === null ? '' : `, judged ${row.total}/300`})`;
    lines.push(`--- Solution ${label}${rank} ---`, String(row.text || ''), '');
  });
  lines.push('Take the best ideas from all of them where they are complementary, not '
    + 'conflicting, and weave a hybrid that is better than any single one.');
  lines.push('Write the final answer. Where the solutions conflict, pick one and say why in a line.');
  return lines.join('\n');
}

// ── the setup form ─────────────────────────────────────────────────────────

function modelPickerHtml(rows, selected, max) {
  const chosen = Array.isArray(selected) ? selected.map(String) : [];
  const cap = trNum(max, TRN_MAX_MODELS) || TRN_MAX_MODELS;
  const list = Array.isArray(rows) ? rows : [];
  if (!list.length) {
    return '<div class="trn-empty">No models found. Add an endpoint in Settings first.</div>';
  }
  const items = list.map(row => {
    const id = String((row && row.id) || '');
    const on = chosen.indexOf(id) !== -1;
    const full = chosen.length >= cap && !on;
    return `<label class="trn-model${on ? ' is-on' : ''}${full ? ' is-full' : ''}">`
      + `<input type="checkbox" data-trn-model="${trEsc(id)}"${on ? ' checked' : ''}`
      + `${full ? ' disabled' : ''}>`
      + `<span class="trn-model-name">${trEsc((row && row.name) || id)}</span>`
      + ((row && row.endpoint)
        ? `<span class="trn-model-ep">${trEsc(row.endpoint)}</span>` : '')
      + '</label>';
  }).join('');
  return `<div class="trn-models" data-trn-models>${items}</div>`
    + `<div class="trn-models-count">${chosen.length} of ${cap} picked`
    + `${chosen.length < TRN_MIN_MODELS ? ` · pick at least ${TRN_MIN_MODELS}` : ''}</div>`;
}

function judgePickerHtml(selected, judge) {
  const chosen = Array.isArray(selected) ? selected.map(String) : [];
  const options = ['<option value="">strongest of the entrants</option>'].concat(
    chosen.map(id => `<option value="${trEsc(id)}"${String(judge) === id ? ' selected' : ''}>`
      + `${trEsc(id)}</option>`));
  return `<select class="trn-select" data-trn-judge aria-label="Judge model">${options.join('')}</select>`;
}

function setupHtml(state) {
  const s = state && typeof state === 'object' ? state : {};
  const rows = Array.isArray(s.models) ? s.models : [];
  const selected = Array.isArray(s.selected) ? s.selected : [];
  const rounds = trNum(s.rounds, 3) || 3;
  const cap = trNum(s.max_models, TRN_MAX_MODELS) || TRN_MAX_MODELS;
  const busy = Boolean(s.starting);
  const ready = selected.length >= TRN_MIN_MODELS && String(s.prompt || '').trim() && !busy;
  const off = s.enabled === false;
  return '<section class="trn-setup">'
    + '<header class="trn-head"><h3>Tournament</h3>'
    + `<p class="trn-sub">The same prompt to ${TRN_MIN_MODELS}–${cap} models, blind and in `
    + 'parallel. Then every model sees all the answers anonymised and weaves the '
    + 'complementary parts into a hybrid. Only DIFFERENT models generate at the same '
    + 'time — two entries on one model take turns.</p></header>'
    + (off ? '<div class="trn-note is-off">The tournament is switched off in '
      + 'Settings → Agent &amp; automation. Past runs still open.</div>' : '')
    + `<div class="trn-error"${s.error ? '' : ' hidden'} data-trn-error>${trEsc(s.error || '')}</div>`
    + '<form data-trn-form>'
    + '<label class="trn-label">Prompt</label>'
    + `<textarea class="trn-prompt" data-trn-prompt rows="5" aria-label="Tournament prompt"`
    + ` placeholder="The task every model answers…">${trEsc(s.prompt || '')}</textarea>`
    + '<label class="trn-label">Models</label>'
    + modelPickerHtml(rows, selected, cap)
    + '<div class="trn-row">'
    + '<label class="trn-label">Rounds (max)</label>'
    + `<input class="trn-input" type="number" min="1" max="6" value="${trEsc(String(rounds))}"`
    + ' data-trn-rounds aria-label="Maximum rounds">'
    + '<label class="trn-label">Judge</label>'
    + judgePickerHtml(selected, s.judge)
    + '</div>'
    + '<p class="trn-hint">Rounds is a maximum: the run stops by itself as soon as the '
    + 'rounds stop changing anything.</p>'
    + `<button type="submit" class="trn-btn trn-run-btn" data-trn-run${ready ? '' : ' disabled'}>`
    + `${busy ? 'Starting…' : 'Run tournament'}</button>`
    + '</form>'
    + runListHtml(s.runs)
    + '</section>';
}

function runListHtml(runs) {
  const rows = Array.isArray(runs) ? runs : [];
  if (!rows.length) return '';
  const items = rows.slice(0, 20).map(row => {
    const r = row && typeof row === 'object' ? row : {};
    return `<button type="button" class="trn-past" data-trn-open="${trEsc(r.id || '')}">`
      + `<span class="trn-past-prompt">${trEsc(r.prompt || '(no prompt)')}</span>`
      + `<span class="trn-past-meta">${trEsc((r.models || []).join(' · '))}</span>`
      + `<span class="trn-past-status trn-state-${trToken(r.status)}">${trEsc(r.status || '')}</span>`
      + '</button>';
  }).join('');
  return `<div class="trn-past-list"><h4>Earlier tournaments</h4>${items}</div>`;
}

// ── the board: one card per model, filling in per round ─────────────────────

function stateOfEntry(run, entry, rows) {
  const progress = (run && run.progress) || [];
  for (const p of progress) {
    if (p && trNum(p.entry, -1) === entry) return String(p.state || 'queued');
  }
  for (const e of (run && run.errors) || []) {
    if (e && trNum(e.entry, -1) === entry) return 'error';
  }
  for (const c of (run && run.cancelled) || []) {
    if (c && trNum(c.entry, -1) === entry) return 'cancelled';
  }
  return rows && rows.length ? 'answered' : 'queued';
}

function detailOfEntry(run, entry) {
  for (const e of (run && run.errors) || []) {
    if (e && trNum(e.entry, -1) === entry) return String(e.error || 'failed');
  }
  for (const c of (run && run.cancelled) || []) {
    if (c && trNum(c.entry, -1) === entry) return String(c.reason || 'stopped');
  }
  return '';
}

function modelCardHtml(run, entry, model, rows, opts) {
  const o = opts && typeof opts === 'object' ? opts : {};
  const answers = Array.isArray(rows) ? rows : [];
  const openRound = trInt(o.round);
  const shown = answers.length
    ? (answers.find(a => a.round === openRound) || answers[answers.length - 1])
    : null;
  const state = stateOfEntry(run, entry, answers);
  const detail = detailOfEntry(run, entry);
  const chips = answers.map(a => {
    const on = shown && a.round === shown.round;
    return `<button type="button" class="trn-round-chip${on ? ' is-on' : ''}"`
      + ` data-trn-round="${trEsc(String(a.round))}" data-trn-entry="${trEsc(String(entry))}">`
      + `${a.round === 0 ? 'blind' : `round ${trEsc(String(a.round))}`}</button>`;
  }).join('');
  return `<article class="trn-card is-${trToken(state)}" data-trn-card="${trEsc(String(entry))}">`
    + '<header class="trn-card-head">'
    + `<span class="trn-card-model">${trEsc(model)}</span>`
    + `<span class="trn-card-state trn-state-${trToken(state)}">${trEsc(state)}</span>`
    + '</header>'
    + (chips ? `<div class="trn-rounds">${chips}</div>` : '')
    + (detail ? `<p class="trn-card-detail">${trEsc(detail)}</p>` : '')
    + (shown
      ? `<pre class="trn-answer">${trEsc(shown.text)}</pre>`
      + `<footer class="trn-card-foot">${trEsc(String(shown.text.length))} chars · `
      + `${trEsc(shown.elapsed_s.toFixed(1))}s · ${trEsc(String(shown.tokens))} tokens`
      + `${shown.tokens_source === 'estimated' ? ' (estimated)' : ''}</footer>`
      : '<div class="trn-answer is-waiting">waiting for this model…</div>')
    + '</article>';
}

function boardHtml(run, opts) {
  const r = run && typeof run === 'object' ? run : null;
  if (!r) return '<div class="trn-empty">No tournament loaded.</div>';
  const o = opts && typeof opts === 'object' ? opts : {};
  const byEntry = answersByEntry(r);
  const cards = (r.models || []).map((model, i) =>
    modelCardHtml(r, i, model, byEntry.get(i) || [], o)).join('');
  const stopped = stoppedByLabel(r);
  return '<section class="trn-board">'
    + '<header class="trn-board-head">'
    + `<span class="trn-board-status trn-state-${trToken(r.status)}">${trEsc(r.status)}</span>`
    + `<span class="trn-board-meta">${trEsc(String(r.rounds_run))} of ${trEsc(String(r.rounds))} rounds`
    + ` · ${trEsc(r.duration_s.toFixed(1))}s</span>`
    + (stopped ? `<span class="trn-stopped${r.stopped_by === 'convergence' ? ' is-converged' : ''}">`
      + `${trEsc(stopped)}</span>` : '')
    + (isLiveStatus(r.status)
      ? '<button type="button" class="trn-btn trn-cancel-btn" data-trn-cancel>Stop</button>'
      : '<button type="button" class="trn-btn trn-back-btn" data-trn-back>New tournament</button>')
    + '</header>'
    + (r.error ? `<div class="trn-error">${trEsc(r.error)}</div>` : '')
    + `<div class="trn-cards">${cards}</div>`
    + resultsTableHtml(r)
    + '</section>';
}

// ── the results table ──────────────────────────────────────────────────────

function resultsTableHtml(run) {
  const r = run && typeof run === 'object' ? run : null;
  const finals = (r && r.final) || [];
  if (!finals.length) return '';
  const label = rankingLabel(r);
  const rows = finals.map(row => {
    const win = row.rank === 1;
    return `<tr class="trn-result-row${win ? ' is-winner' : ''} is-${trToken(row.outcome)}"`
      + ` data-trn-result="${trEsc(String(row.entry))}">`
      + `<td class="trn-rank">${row.rank === null ? '—' : trEsc(String(row.rank))}</td>`
      + `<td class="trn-result-model">${trEsc(row.model)}`
      + (win ? '<span class="trn-crown" aria-label="winner">★</span>' : '')
      + (row.outcome !== 'success' ? `<span class="trn-outcome">${trEsc(row.outcome)}</span>` : '')
      + '</td>'
      + TRN_AXES.map(axis => scoreCellHtml(row.scores[axis])).join('')
      + `<td class="trn-total">${row.total === null ? '—' : trEsc(String(row.total))}</td>`
      + `<td class="trn-tiebreak">${trEsc(row.tiebreak.toFixed(3))}</td>`
      + '</tr>';
  }).join('');
  const judge = r.judge || {};
  return '<section class="trn-results">'
    + '<h4>Result</h4>'
    + '<table class="trn-table"><thead><tr>'
    + '<th>Rank</th><th>Model</th><th>Correct</th><th>Complete</th><th>Sophist.</th>'
    + '<th>Total</th><th title="deterministic: key-term coverage and answer length">Tiebreak</th>'
    + `</tr></thead><tbody>${rows}</tbody></table>`
    + `<p class="trn-ranking is-${trToken(label.kind)}">${trEsc(label.text)}</p>`
    + (judge.model
      ? `<p class="trn-judge">judge: ${trEsc(judge.model)}`
      + `${judge.ok ? '' : ` — ${trEsc(judge.error || 'no judgement')}`}`
      + `${trNum(judge.attempts, 0) > 1 ? ` (${trEsc(String(judge.attempts))} attempts)` : ''}</p>`
      : '')
    + (r.convergence
      ? `<p class="trn-convergence">${trEsc(String(r.convergence.reason || ''))}</p>` : '')
    + '<button type="button" class="trn-btn trn-merge-btn" data-trn-merge>'
    + 'Merge into the composer</button>'
    + '</section>';
}
// ── Tournament: end pure helpers ──

export {
  setupHtml, boardHtml, resultsTableHtml, mergePromptFor, normalizeRun,
  modelRowsFrom, rankingLabel, stoppedByLabel,
};

const $ = (id) => document.getElementById(id);

const MODAL_ID = 'tournament-modal';
const SETUP_ID = 'tournament-setup';
const BOARD_ID = 'tournament-board';

let _wired = false;
let _returnFocus = null;
let _timer = null;
let _setup = {
  models: [], selected: [], prompt: '', rounds: 3, judge: '', error: '',
  starting: false, enabled: true, max_models: TRN_MAX_MODELS, runs: [],
};
let _run = null;
let _view = { round: null };

/** fetch wrapper for /api/tournament/*: a non-2xx becomes an Error with {detail}. */
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

export const listTournaments = () => req('?limit=20');
export const readTournament = (id) => req(`/${encodeURIComponent(id)}`);
export const startTournament = (body) => req('', { method: 'POST', body: JSON.stringify(body) });
export const cancelTournament = (id) =>
  req(`/${encodeURIComponent(id)}/cancel`, { method: 'POST' });

async function loadModels() {
  const res = await fetch(MODELS_API, { credentials: 'same-origin' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return modelRowsFrom(await res.json());
}

function inlineError(host, message) {
  const box = host && host.querySelector('[data-trn-error]');
  if (!box) return;
  box.textContent = String(message == null ? '' : message);
  box.hidden = !String(message || '');
}

// ── rendering ──────────────────────────────────────────────────────────────

function showView(which) {
  const setup = $(SETUP_ID);
  const board = $(BOARD_ID);
  if (setup) setup.classList.toggle('hidden', which !== 'setup');
  if (board) board.classList.toggle('hidden', which !== 'board');
}

function renderSetup() {
  const host = $(SETUP_ID);
  if (!host) return;
  host.innerHTML = setupHtml(_setup);
  showView('setup');
}

function renderBoard() {
  const host = $(BOARD_ID);
  if (!host) return;
  host.innerHTML = boardHtml(_run, _view);
  showView('board');
}

// ── actions ────────────────────────────────────────────────────────────────

async function refreshSetup() {
  try {
    const [models, listing] = await Promise.all([
      loadModels().catch(() => []),
      listTournaments().catch(() => null),
    ]);
    _setup.models = models;
    if (listing) {
      _setup.runs = Array.isArray(listing.runs) ? listing.runs : [];
      if (listing.enabled !== undefined) _setup.enabled = Boolean(listing.enabled);
      if (listing.max_models) _setup.max_models = trNum(listing.max_models, TRN_MAX_MODELS);
    }
  } catch (error) {
    _setup.error = String(error && error.message ? error.message : error);
  }
  renderSetup();
}

function captureSetup(host) {
  if (!host) return;
  const prompt = host.querySelector('[data-trn-prompt]');
  if (prompt) _setup.prompt = prompt.value;
  const rounds = host.querySelector('[data-trn-rounds]');
  if (rounds) _setup.rounds = trNum(rounds.value, 3) || 3;
  const judge = host.querySelector('[data-trn-judge]');
  if (judge) _setup.judge = judge.value || '';
}

function toggleModel(id) {
  const key = String(id || '');
  const at = _setup.selected.indexOf(key);
  if (at === -1) {
    if (_setup.selected.length >= (_setup.max_models || TRN_MAX_MODELS)) return;
    _setup.selected.push(key);
  } else {
    _setup.selected.splice(at, 1);
  }
  if (_setup.judge && _setup.selected.indexOf(_setup.judge) === -1) _setup.judge = '';
  renderSetup();
}

async function startRun() {
  const host = $(SETUP_ID);
  captureSetup(host);
  if (_setup.selected.length < TRN_MIN_MODELS) {
    inlineError(host, `Pick at least ${TRN_MIN_MODELS} models.`);
    return;
  }
  if (!String(_setup.prompt || '').trim()) {
    inlineError(host, 'Write the prompt every model should answer.');
    return;
  }
  _setup.starting = true;
  _setup.error = '';
  renderSetup();
  try {
    const started = await startTournament({
      prompt: _setup.prompt,
      models: _setup.selected,
      rounds: _setup.rounds,
      judge_model: _setup.judge || undefined,
    });
    _run = normalizeRun(started);
    _view = { round: null };
    renderBoard();
    schedulePoll();
  } catch (error) {
    _setup.error = String(error && error.message ? error.message : error);
    renderSetup();
  } finally {
    _setup.starting = false;
  }
}

function stopPolling() {
  if (_timer) { clearTimeout(_timer); _timer = null; }
}

function schedulePoll() {
  stopPolling();
  if (!_run || !isLiveStatus(_run.status)) return;
  _timer = setTimeout(poll, POLL_MS);
}

async function poll() {
  if (!_run || !_run.id) return;
  try {
    _run = normalizeRun(await readTournament(_run.id));
    renderBoard();
  } catch (error) {
    const host = $(BOARD_ID);
    if (host) inlineError(host, String(error && error.message ? error.message : error));
  }
  schedulePoll();
}

async function openRun(id) {
  stopPolling();
  try {
    _run = normalizeRun(await readTournament(id));
    _view = { round: null };
    renderBoard();
    schedulePoll();
  } catch (error) {
    _setup.error = String(error && error.message ? error.message : error);
    renderSetup();
  }
}

async function stopRun() {
  if (!_run || !_run.id) return;
  try {
    await cancelTournament(_run.id);
    await poll();
  } catch (error) {
    const host = $(BOARD_ID);
    if (host) inlineError(host, String(error && error.message ? error.message : error));
  }
}

/** "Merge": assemble the synthesis prompt from the finalists and drop it into
 *  the composer — the user decides when to send it. */
function mergeIntoComposer() {
  const text = mergePromptFor(_run);
  const input = $('message') || $('message-input');
  if (!text || !input) {
    const host = $(BOARD_ID);
    if (host) inlineError(host, !text ? 'There is nothing to merge yet.' : 'Chat composer not found.');
    return;
  }
  input.value = text;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  closeTournamentPanel();
  try { input.focus(); } catch (_) { /* focus is a courtesy */ }
  try { uiModule?.showToast?.('Merge prompt dropped into the composer'); } catch (_) { /* optional */ }
}

// ── wiring: one delegated listener set on the modal ─────────────────────────

function wire() {
  if (_wired) return;
  const modal = $(MODAL_ID);
  if (!modal) return;
  _wired = true;

  modal.addEventListener('click', (event) => {
    const target = event.target;
    if (target.closest('#close-tournament-modal')) { closeTournamentPanel(); return; }
    if (target.closest('[data-trn-back]')) { stopPolling(); renderSetup(); return; }
    if (target.closest('[data-trn-cancel]')) { stopRun(); return; }
    if (target.closest('[data-trn-merge]')) { mergeIntoComposer(); return; }
    const past = target.closest('[data-trn-open]');
    if (past) { openRun(past.dataset.trnOpen); return; }
    const chip = target.closest('[data-trn-round]');
    if (chip) {
      _view = { round: trInt(chip.dataset.trnRound), entry: trInt(chip.dataset.trnEntry) };
      renderBoard();
    }
  });

  modal.addEventListener('change', (event) => {
    const box = event.target.closest('[data-trn-model]');
    if (!box) return;
    captureSetup($(SETUP_ID));
    toggleModel(box.dataset.trnModel);
  });

  modal.addEventListener('submit', (event) => {
    if (!event.target.closest('[data-trn-form]')) return;
    event.preventDefault();
    startRun();
  });

  $('tool-tournament-btn')?.addEventListener('click', () => openTournamentPanel());
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const open = $(MODAL_ID);
    if (open && !open.classList.contains('hidden')) closeTournamentPanel();
  });
}

export async function openTournamentPanel(options = {}) {
  const modal = $(MODAL_ID);
  if (!modal) return;
  wire();
  _returnFocus = document.activeElement;
  modal.classList.remove('hidden');
  if (options.id) { await openRun(options.id); return; }
  if (_run && isLiveStatus(_run.status)) { renderBoard(); schedulePoll(); return; }
  await refreshSetup();
}

export function closeTournamentPanel() {
  stopPolling();
  $(MODAL_ID)?.classList.add('hidden');
  try { _returnFocus?.focus?.(); } catch (_) { /* focus is a courtesy */ }
}

export function initTournament() {
  wire();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initTournament);
} else {
  initTournament();
}

const tournamentModule = {
  initTournament,
  openTournamentPanel,
  closeTournamentPanel,
  listTournaments,
  readTournament,
  startTournament,
  cancelTournament,
  mergePromptFor,
  normalizeRun,
};

if (typeof window !== 'undefined') window.tournamentModule = tournamentModule;

export default tournamentModule;
