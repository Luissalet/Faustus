'use strict';
// Which turns are alive, which wait for the owner and which finished unseen:
// the state behind the tray icon, its menu and the "turn finished" alert.
//
// Pure on purpose (`node --test turn-status.test.cjs`): no Electron, no
// network, and no clock but the server time each snapshot carries (`ts`), so
// a Faustus on another machine (a Spark) and this PC never compare clocks.
// desktop/turn-tray.cjs does the polling, the icon, the toast and the voice.

const DEFAULTS = Object.freeze({notify: true, voice: false, minSeconds: 60});
const RECENT_MAX = 8;            // finished turns kept in the menu
const RECENT_TTL_S = 12 * 3600;  // ...and for how long
const QUESTION_FRESH_S = 24 * 3600; // an older open question is history, not something waiting right now
const SETTING_KEYS = Object.freeze({notify: 'desktop_turn_notify_enabled', voice: 'desktop_turn_voice_enabled', minSeconds: 'desktop_turn_notify_min_seconds'});

const arr = v => Array.isArray(v) ? v : [];
const str = v => v === null || v === undefined ? '' : String(v);
const num = v => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
const obj = v => v && typeof v === 'object' && !Array.isArray(v) ? v : {};

/** The three settings (Settings > This device), with defaults for a server that does not know them yet. */
function settingsFrom(raw) {
  const s = obj(raw);
  const flag = (v, fallback) => v === undefined || v === null || v === '' ? fallback : (v === true || v === 'true' || v === 1 || v === '1');
  let min = s[SETTING_KEYS.minSeconds];
  min = min === undefined || min === null || min === '' || !Number.isFinite(Number(min)) ? DEFAULTS.minSeconds : Number(min);
  return {
    notify: flag(s[SETTING_KEYS.notify], DEFAULTS.notify),
    voice: flag(s[SETTING_KEYS.voice], DEFAULTS.voice),
    minSeconds: Math.max(0, Math.min(Math.round(min), 3600)),
  };
}

function isoSeconds(value) {
  if (typeof value === 'number') return value;
  const text = str(value).trim();
  if (!text) return 0;
  const ms = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(text) ? text : text + 'Z');
  return Number.isFinite(ms) ? ms / 1000 : 0;
}

/**
 * One picture out of `GET /api/chat/activity` (+ `GET /api/questions` when it
 * was asked this round; `null` keeps the questions of the last round).
 * Delegate workers are left out: they are part of their parent's turn.
 */
function snapshotFrom(activity, questions, fallbackNow) {
  const a = obj(activity), details = obj(a.details), runs = obj(a.runs), queued = obj(a.queued), workers = obj(a.workers);
  const ts = num(a.ts) || num(fallbackNow) || Date.now() / 1000;
  const running = [];
  for (const raw of arr(a.running)) {
    const id = str(raw);
    if (!id || workers[id]) continue;
    const d = obj(details[id]);
    running.push({id, runId: str(runs[id] || d.run_id), label: str(d.label).trim(), startedAt: num(d.started_at), phase: str(d.phase_canonical || d.phase), queued: num(queued[id])});
  }
  let open = null;
  if (questions !== null && questions !== undefined) {
    open = [];
    for (const q of arr(obj(questions).questions)) {
      let id = str(obj(q).session);
      if (id.startsWith('mcp:')) continue;               // no chat to open
      if (id.startsWith('code_mode:')) id = id.slice(10); // asked in the middle of that chat's turn
      const openedAt = isoSeconds(q.opened_at);
      if (!id || (openedAt && ts - openedAt > QUESTION_FRESH_S)) continue;
      open.push({id, text: str(q.question).trim(), openedAt});
    }
  }
  return {ts, reliable: !(num(a.unverified) > 0), running, approvals: arr(a.awaiting_approval).map(str).filter(Boolean), questions: open};
}

function createState(saved) {
  const state = {turns: new Map(), questions: [], ts: 0, primed: false};
  for (const t of arr(obj(saved).turns)) {
    if (!t || !t.id || !['done', 'failed', 'stopped'].includes(t.state)) continue;
    state.turns.set(String(t.id), {id: String(t.id), runId: str(t.runId), label: str(t.label), startedAt: num(t.startedAt), finishedAt: num(t.finishedAt),
      state: t.state, waiting: null, question: '', phase: '', queued: 0, seen: Boolean(t.seen), summary: str(t.summary), error: str(t.error)});
  }
  return state;
}

/** What survives a restart of the app: the finished turns (their "unseen" mark with them). */
function serialize(state) {
  return {turns: [...state.turns.values()].filter(t => ['done', 'failed', 'stopped'].includes(t.state))
    .map(({id, runId, label, startedAt, finishedAt, state: s, seen, summary, error}) => ({id, runId, label, startedAt, finishedAt, state: s, seen, summary, error}))};
}

const live = t => t.state === 'running' || t.state === 'waiting' || t.state === 'settling';
const finished = t => t.state === 'done' || t.state === 'failed' || t.state === 'stopped';

/** True when a turn this state follows is gone from `activity.running`: the questions must be read before deciding it ended. */
function needsQuestions(state, activity) {
  const now = new Set(arr(obj(activity).running).map(str));
  for (const t of state.turns.values()) if ((t.state === 'running' || (t.state === 'waiting' && t.inRun)) && !now.has(t.id)) return true;
  return false;
}

function watching(ctx, id) { return Boolean(ctx && ctx.focused && ctx.visibleId === id); }

/**
 * Folds one snapshot into the state. Returns the effects the caller owes:
 *   {type:'outcome', id}            the run is gone: read how it ended, then call settle()
 *   {type:'label', id}              a waiting chat this state has no name for
 *   {type:'notify'|'speak', ...}    a turn that stopped to wait for the owner
 * Nothing is announced on the first snapshot after a start (what was already waiting is not news).
 */
function observe(state, snap, ctx) {
  const effects = [], settings = (ctx && ctx.settings) || DEFAULTS, L = strings(ctx && ctx.lang);
  if (snap.questions) state.questions = snap.questions;
  state.ts = snap.ts;
  const approvals = new Set(snap.approvals), question = new Map(state.questions.map(q => [q.id, q]));
  const runningNow = new Set();
  const enterWaiting = (t, kind, text) => {
    const was = t.waiting;
    t.state = 'waiting'; t.waiting = kind; t.question = kind === 'question' ? text : '';
    if (was === kind || !state.primed) return;
    if (!t.label) effects.push({type: 'label', id: t.id});
    if (watching(ctx, t.id)) return;
    if (settings.notify) effects.push({type: 'notify', kind: 'waiting', id: t.id});
    if (settings.voice) effects.push({type: 'speak', kind: 'waiting', id: t.id});
  };
  for (const r of snap.running) {
    runningNow.add(r.id);
    let t = state.turns.get(r.id);
    if (!t || finished(t) || (t.runId && r.runId && t.runId !== r.runId)) {
      t = {id: r.id, runId: r.runId, label: r.label || (t && t.label) || '', startedAt: r.startedAt || snap.ts, finishedAt: 0, state: 'running', waiting: null, question: '', phase: '', queued: 0, seen: false, summary: '', error: ''};
      state.turns.set(r.id, t);
    }
    if (r.runId) t.runId = r.runId;
    if (r.label) t.label = r.label;
    if (r.startedAt) t.startedAt = r.startedAt;
    t.phase = r.phase; t.queued = r.queued; t.inRun = true;
    const kind = approvals.has(r.id) ? 'approval' : question.has(r.id) ? 'question' : null;
    if (kind) enterWaiting(t, kind, kind === 'question' ? question.get(r.id).text : '');
    else { t.state = 'running'; t.waiting = null; t.question = ''; }
  }
  // Parked on a card or a question with no run behind it (the turn ended by asking).
  for (const id of new Set([...approvals, ...question.keys()])) {
    if (runningNow.has(id)) continue;
    let t = state.turns.get(id);
    const kind = approvals.has(id) ? 'approval' : 'question';
    const q = question.get(id);
    if (!t || finished(t)) {
      t = {id, runId: '', label: (t && t.label) || '', startedAt: (q && q.openedAt) || snap.ts, finishedAt: 0, state: 'running', waiting: null, question: '', phase: '', queued: 0, seen: false, summary: '', error: ''};
      state.turns.set(id, t);
      if (!t.label) effects.push({type: 'label', id});
    }
    t.inRun = false;
    enterWaiting(t, kind, q ? q.text : '');
  }
  if (snap.reliable) {
    for (const t of [...state.turns.values()]) {
      if (runningNow.has(t.id) || approvals.has(t.id) || question.has(t.id)) continue;
      if (t.state === 'waiting' && !t.inRun) state.turns.delete(t.id);   // answered or withdrawn, and nothing runs
      else if (t.state === 'running' || t.state === 'waiting') { t.state = 'settling'; t.waiting = null; effects.push({type: 'outcome', id: t.id}); }
    }
  }
  if (!state.primed) for (const e of effects.splice(0)) if (e.type === 'outcome' || e.type === 'label') effects.push(e);
  state.primed = true;
  prune(state, snap.ts);
  for (const e of effects) if (e.type === 'notify' || e.type === 'speak') Object.assign(e, alertText(state.turns.get(e.id), e.type, L));
  return effects;
}

/**
 * How a run that left the registry ended: `outcome` is {status, finishedAt, text, name, error} read from the server
 * (status: done | error | stopped | running | unknown). Returns the notify / speak effects it deserves.
 */
function settle(state, id, outcome, ctx) {
  const t = state.turns.get(id), o = obj(outcome), settings = (ctx && ctx.settings) || DEFAULTS, L = strings(ctx && ctx.lang);
  if (!t || t.state !== 'settling') return [];
  if (o.status === 'running') { t.state = 'running'; return []; }   // a new run of the same chat already took over
  t.label = t.label || str(o.name).trim();
  t.finishedAt = num(o.finishedAt) || state.ts || t.startedAt;
  t.state = o.status === 'error' ? 'failed' : o.status === 'stopped' ? 'stopped' : 'done';
  t.summary = firstSentence(o.text, 180);
  t.error = t.state === 'failed' ? firstSentence(o.error, 140) : '';
  t.waiting = null; t.question = ''; t.phase = ''; t.queued = 0;
  // Stopping it was the owner's own click; a turn that ends in front of them needs no mark either.
  t.seen = t.state === 'stopped' || watching(ctx, id);
  prune(state, state.ts);
  const effects = [];
  const long = Math.max(0, t.finishedAt - t.startedAt) >= settings.minSeconds;
  if (t.state !== 'stopped' && long && !(ctx && ctx.focused)) {
    if (settings.notify) effects.push({type: 'notify', kind: t.state, id, ...alertText(t, 'notify', L)});
    if (settings.voice) effects.push({type: 'speak', kind: t.state, id, ...alertText(t, 'speak', L)});
  }
  return effects;
}

function setLabel(state, id, name) {
  const t = state.turns.get(id);
  if (t && !t.label && str(name).trim()) { t.label = str(name).trim(); return true; }
  return false;
}

/** The owner looked at that chat (opened it from the tray or the toast, or has it on screen). */
function markSeen(state, id) {
  const t = state.turns.get(id);
  if (!t || !finished(t) || t.seen) return false;
  t.seen = true;
  return true;
}

function prune(state, now) {
  const done = [...state.turns.values()].filter(finished).sort((a, b) => b.finishedAt - a.finishedAt);
  done.forEach((t, index) => { if (index >= RECENT_MAX || (now && now - t.finishedAt > RECENT_TTL_S)) state.turns.delete(t.id); });
}

/** Counts and the one word the icon shows: waiting > failed > running > done > idle. */
function summary(state) {
  const all = [...state.turns.values()];
  const waiting = all.filter(t => t.state === 'waiting').length;
  const running = all.filter(t => t.state === 'running' || t.state === 'settling').length;
  const failed = all.filter(t => t.state === 'failed' && !t.seen).length;
  const unseen = all.filter(t => t.state === 'done' && !t.seen).length;
  const kind = waiting ? 'waiting' : failed ? 'failed' : running ? 'running' : unseen ? 'done' : 'idle';
  // A second, smaller mark when the main one hides something that is still unseen.
  const corner = (kind === 'waiting' || kind === 'running') ? (failed ? 'failed' : unseen ? 'done' : '') : '';
  const count = kind === 'waiting' ? waiting : kind === 'failed' ? failed : kind === 'running' ? running : kind === 'done' ? unseen : 0;
  return {kind, count, corner, waiting, running, failed, unseen};
}

// ── words ───────────────────────────────────────────────────────────────────

const STRINGS = {
  es: {
    chat: 'un chat', idle: 'nada en marcha', offline: 'sin conexión', signedOut: 'inicia sesión en la ventana',
    running: n => n === 1 ? '1 turno en marcha' : `${n} turnos en marcha`,
    waiting: n => n === 1 ? '1 te espera' : `${n} te esperan`,
    failed: n => n === 1 ? '1 ha fallado' : `${n} han fallado`,
    unseen: n => n === 1 ? '1 terminado sin ver' : `${n} terminados sin ver`,
    sectionWaiting: 'Te esperan', sectionRunning: 'En marcha', sectionRecent: 'Terminados',
    approval: 'aprobación pendiente', question: 'te pregunta', queued: n => `en cola (${n})`, settling: 'terminando',
    done: ago => `terminado hace ${ago}`, failedAgo: ago => `falló hace ${ago}`, stopped: ago => `detenido hace ${ago}`, unseenMark: 'sin ver',
    titleDone: label => `Terminado: ${label}`, titleFailed: label => `Ha fallado: ${label}`, titleWaiting: label => `Te espera: ${label}`,
    bodyDone: 'El turno ha terminado.', bodyFailed: 'El turno terminó con un error.', bodyApproval: 'Hay una aprobación pendiente.', bodyQuestion: 'Tiene una pregunta para ti.',
    sayDone: label => `Faustus ha terminado ${label}.`, sayFailed: label => `El turno de ${label} ha fallado.`, sayWaiting: label => `Faustus te espera en ${label}.`,
    notifyToggle: 'Avisar cuando termina un turno largo', voiceToggle: 'Avisar en voz',
    testTitle: 'Aviso de prueba', testBody: 'Así avisa Faustus cuando termina un turno largo.', testSay: 'Faustus avisa así cuando termina un turno largo.',
    s: n => `${n} s`, min: n => `${n} min`, h: (h, m) => m ? `${h} h ${m} min` : `${h} h`, under: 'menos de 1 min',
  },
  en: {
    chat: 'a chat', idle: 'nothing running', offline: 'offline', signedOut: 'sign in from the window',
    running: n => n === 1 ? '1 turn running' : `${n} turns running`,
    waiting: n => n === 1 ? '1 waiting for you' : `${n} waiting for you`,
    failed: n => n === 1 ? '1 failed' : `${n} failed`,
    unseen: n => n === 1 ? '1 finished, unseen' : `${n} finished, unseen`,
    sectionWaiting: 'Waiting for you', sectionRunning: 'Running', sectionRecent: 'Finished',
    approval: 'approval pending', question: 'asks', queued: n => `queued (${n})`, settling: 'finishing',
    done: ago => `finished ${ago} ago`, failedAgo: ago => `failed ${ago} ago`, stopped: ago => `stopped ${ago} ago`, unseenMark: 'unseen',
    titleDone: label => `Finished: ${label}`, titleFailed: label => `Failed: ${label}`, titleWaiting: label => `Waiting for you: ${label}`,
    bodyDone: 'The turn has finished.', bodyFailed: 'The turn ended with an error.', bodyApproval: 'An approval is pending.', bodyQuestion: 'It has a question for you.',
    sayDone: label => `Faustus has finished ${label}.`, sayFailed: label => `The turn of ${label} has failed.`, sayWaiting: label => `Faustus is waiting for you in ${label}.`,
    notifyToggle: 'Alert when a long turn finishes', voiceToggle: 'Spoken alert',
    testTitle: 'Test alert', testBody: 'This is how Faustus tells you a long turn has finished.', testSay: 'This is how Faustus tells you a long turn has finished.',
    s: n => `${n} s`, min: n => `${n} min`, h: (h, m) => m ? `${h} h ${m} min` : `${h} h`, under: 'under 1 min',
  },
};
function strings(lang) { return STRINGS[String(lang || '').toLowerCase().startsWith('en') ? 'en' : 'es']; }

function duration(seconds, L) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return L.s(s);
  const m = Math.round(s / 60);
  return m < 60 ? L.min(m) : L.h(Math.floor(m / 60), m % 60);
}

/** To the minute, for a menu that should not be rebuilt every few seconds. */
function coarse(seconds, L) { return seconds < 60 ? L.under : duration(seconds, L); }

function clip(text, max) {
  const t = str(text).replace(/\s+/g, ' ').trim();
  if (t.length <= max) return t;
  const cut = t.slice(0, max - 1), space = cut.lastIndexOf(' ');
  return (space > max * 0.6 ? cut.slice(0, space) : cut).replace(/[\s,;:.]+$/, '') + '…';
}

/** A reply as plain lines: no code blocks, reasoning, links, emphasis marks or table rules. */
function plainLines(text) {
  let t = Array.isArray(text) ? text.map(p => typeof p === 'string' ? p : str(obj(p).text)).join('\n') : str(text);
  t = t.replace(/<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>/gi, ' ').replace(/```[\s\S]*?(```|$)/g, ' ')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/<[^>]{1,200}>/g, ' ');
  const lines = [];
  for (const raw of t.split(/\r?\n/)) {
    if (/^\s*\|?[\s:|-]{3,}\|?\s*$/.test(raw)) continue;   // table rule, horizontal rule
    const heading = /^\s{0,3}#{1,6}\s/.test(raw);
    const line = raw.replace(/^\s{0,3}#{1,6}\s+/, '').replace(/^\s*(?:[-*+•]|\d+[.)])\s+/, '').replace(/^\s*>+\s?/, '')
      .replace(/\*\*|__|`+|~~/g, '').replace(/(^|\s)[*_](\S[^*_]*\S|\S)[*_](?=\s|[.,;:!?)]|$)/g, '$1$2').replace(/\|/g, ' ').replace(/\s+/g, ' ').trim();
    if (line) lines.push({line, heading});
  }
  return lines;
}

/** The first sentence of a reply, for a toast body or a spoken line. A bare heading ("Resultado") is skipped when prose follows. */
function firstSentence(text, max = 160) {
  const lines = plainLines(text);
  if (!lines.length) return '';
  let first = lines.find((l, i) => !(l.heading || /[:：]$/.test(l.line) && l.line.length < 40) || i === lines.length - 1) || lines[0];
  let line = first.line, out = '';
  // A full stop ends the sentence only before a space and a capital, a digit or an opening mark ("3.5" and "p. ej." go on).
  const end = /[.!?…]+["'»)\]]*(?=\s+[A-ZÁÉÍÓÚÑÜ¿¡"'«(\d]|\s*$)/g;
  let m;
  while ((m = end.exec(line))) {
    out = line.slice(0, m.index + m[0].length);
    if (out.length >= 24) break;
  }
  return clip(out || line, max);
}

function alertText(turn, type, L) {
  const t = turn || {}, label = clip(t.label, 60) || L.chat;
  if (type === 'speak') {
    if (t.state === 'waiting') return {text: L.sayWaiting(label)};
    if (t.state === 'failed') return {text: L.sayFailed(label)};
    return {text: clip(`${L.sayDone(label)} ${firstSentence(t.summary, 170)}`, 260)};
  }
  if (t.state === 'waiting') return {title: L.titleWaiting(label), body: t.waiting === 'question' ? (clip(t.question, 180) || L.bodyQuestion) : L.bodyApproval};
  if (t.state === 'failed') return {title: L.titleFailed(label), body: t.error || t.summary || L.bodyFailed};
  return {title: L.titleDone(label), body: t.summary || L.bodyDone};
}

/** "Faustus — 2 turnos en marcha · 1 te espera" */
function tooltip(state, lang, link) {
  const L = strings(lang), s = summary(state);
  if (link === 'offline') return `Faustus — ${L.offline}`;
  if (link === 'signed-out') return `Faustus — ${L.signedOut}`;
  const parts = [];
  if (s.waiting) parts.push(L.waiting(s.waiting));
  if (s.running) parts.push(L.running(s.running));
  if (s.failed) parts.push(L.failed(s.failed));
  if (s.unseen) parts.push(L.unseen(s.unseen));
  return `Faustus — ${parts.join(' · ') || L.idle}`;
}

/** The tray menu's turns, most pressing first: [{title, items: [{id, text, unseen}]}]. */
function menu(state, lang, now) {
  const L = strings(lang), at = num(now) || state.ts, all = [...state.turns.values()];
  const name = t => clip(t.label, 46) || L.chat;
  const sections = [];
  const waiting = all.filter(t => t.state === 'waiting').sort((a, b) => a.startedAt - b.startedAt);
  if (waiting.length) sections.push({title: L.sectionWaiting, items: waiting.map(t => ({id: t.id, text: `${name(t)} — ${t.waiting === 'question' ? `${L.question}: ${clip(t.question, 50)}`.replace(/: $/, '') : L.approval}`}))});
  const running = all.filter(t => t.state === 'running' || t.state === 'settling').sort((a, b) => a.startedAt - b.startedAt);
  if (running.length) sections.push({title: L.sectionRunning, items: running.map(t => ({id: t.id, text: `${name(t)} — ${t.state === 'settling' ? L.settling : t.queued > 0 ? L.queued(t.queued) : coarse(at - t.startedAt, L)}`}))});
  const recent = all.filter(finished).sort((a, b) => b.finishedAt - a.finishedAt);
  if (recent.length) sections.push({title: L.sectionRecent, items: recent.map(t => {
    const ago = coarse(at - t.finishedAt, L);
    const how = t.state === 'failed' ? L.failedAgo(ago) : t.state === 'stopped' ? L.stopped(ago) : L.done(ago);
    return {id: t.id, unseen: !t.seen, text: `${t.seen ? '' : '● '}${name(t)} — ${how}`};
  })});
  return sections;
}

module.exports = {DEFAULTS, SETTING_KEYS, RECENT_MAX, settingsFrom, snapshotFrom, createState, serialize, needsQuestions, observe, settle, setLabel, markSeen,
  summary, strings, duration, clip, firstSentence, alertText, tooltip, menu};
