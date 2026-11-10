'use strict';
// The tray icon as a status light, and the alert when a long turn finishes
// with the window out of sight: a Windows notification (click = open that
// chat) and, when the owner turned it on, one spoken sentence.
//
// What runs is read from the Faustus the window is attached to, wherever it
// lives (this PC or a Spark): `GET /api/chat/activity` every few seconds
// while something is alive and rarely otherwise (the same call and pace the
// sidebar dots use), `GET /api/questions` once a minute or when a run goes
// away, and the outcome of a turn (its status and its last reply) only once,
// when it ends. The rules live in turn-status.cjs and the drawing in
// tray-badge.cjs; this file is the Electron side, with every Electron object
// injected so `node --test turn-tray.test.cjs` runs it with fakes.
const {join, dirname} = require('node:path');
const {readFileSync, writeFileSync, mkdirSync, renameSync} = require('node:fs');
const turns = require('./turn-status.cjs');
const badge = require('./tray-badge.cjs');

const ICON_SIZES = [16, 20, 24, 32, 40, 48];
const PLAYER_PAGE = 'data:text/html;charset=utf-8,' + encodeURIComponent('<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; media-src data:"><title>Faustus</title>');

function startTurnTray(electron, options) {
  const {net, Notification, nativeImage, BrowserWindow, ipcMain} = electron;
  const {
    tray, origin, focus = () => ({focused: false, visibleId: ''}), openChat = () => {}, showMain = () => {}, rebuildMenu = () => {}, setOverlay = () => {},
    iconPath = '', stateFile = '', lang = () => 'es', log = () => {}, liveMs = 3000, idleMs = 10000, questionsMs = 60000, settingsMs = 60000, settleDelayMs = 400,
  } = options;

  let saved = null;
  if (stateFile) { try { saved = JSON.parse(readFileSync(stateFile, 'utf8')); } catch { /* first run */ } }
  const state = turns.createState(saved);
  let settings = turns.settingsFrom(null), settingsAt = 0, questionsAt = 0;
  let link = '', failures = 0, timer = null, busy = false, stopped = false, again = false, lastDrawn = '', lastSaved = '';
  let player = null, playerTimer = null, lastVoice = null;
  const voiceWaiters = new Set(), shown = new Set(), images = new Map();

  // ── talking to Faustus ────────────────────────────────────────────────────
  async function request(path, init = {}, timeoutMs = 8000) {
    const controller = new AbortController();
    const abort = setTimeout(() => controller.abort(), timeoutMs);
    try { return await net.fetch(origin() + path, {credentials: 'include', ...init, signal: controller.signal}); }
    finally { clearTimeout(abort); }
  }
  async function read(path, timeoutMs) {
    try {
      const response = await request(path, {}, timeoutMs);
      if (!response.ok) return {status: response.status, body: null};
      return {status: 200, body: await response.json()};
    } catch { return {status: 0, body: null}; }
  }
  async function refreshSettings(maxAgeMs) {
    if (Date.now() - settingsAt < maxAgeMs) return;
    const answer = await read('/api/auth/settings', 5000);
    if (answer.status === 200) { settings = turns.settingsFrom(answer.body); settingsAt = Date.now(); }
  }
  async function saveSetting(name, value) {
    try {
      const response = await request('/api/auth/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({[turns.SETTING_KEYS[name]]: value})});
      if (response.ok) { settings = turns.settingsFrom(await response.json()); settingsAt = Date.now(); }
      else log(`settings: ${turns.SETTING_KEYS[name]} not saved (${response.status})`);
    } catch (error) { log('settings: ' + error.message); }
    draw(true);
  }
  function errorText(error) {
    if (!error) return '';
    if (typeof error === 'string') return error;
    for (const key of ['message', 'error', 'detail', 'content']) if (typeof error[key] === 'string' && error[key].trim()) return error[key];
    return '';
  }
  /** How the run of that chat ended, and what it said last. */
  async function outcome(id) {
    if (settleDelayMs) await new Promise(resolve => setTimeout(resolve, settleDelayMs));   // the reply is stored as the run closes
    const key = encodeURIComponent(id);
    const [run, history] = await Promise.all([read(`/api/session/${key}/objective`, 8000), read(`/api/history/${key}?limit=8`, 8000)]);
    const generation = (run.body && run.body.generation) || {};
    const how = generation.active ? 'running' : ['done', 'error', 'stopped'].includes(generation.state) ? generation.state : 'unknown';
    const messages = (history.body && Array.isArray(history.body.history)) ? history.body.history : [];
    const last = [...messages].reverse().find(m => m && m.role === 'assistant' && m.content && String(typeof m.content === 'string' ? m.content : JSON.stringify(m.content)).trim());
    return {status: how, finishedAt: Number(generation.finished_at) || 0, text: last ? last.content : '', name: (history.body && history.body.name) || generation.label || '', error: errorText(generation.error)};
  }

  // ── the round ─────────────────────────────────────────────────────────────
  const context = () => ({...focus(), settings, lang: lang()});
  async function carryOut(effects) {
    const asked = new Set();
    for (const effect of effects) {
      if (effect.type === 'outcome') {
        await refreshSettings(5000);
        let result;
        try { result = await outcome(effect.id); } catch { result = {status: 'unknown'}; }
        const turn = state.turns.get(effect.id);
        const follow = turns.settle(state, effect.id, result, context());
        if (turn) log(`turn ${effect.id.slice(0, 8)} "${turn.label}" ${turn.state} after ${Math.round(Math.max(0, turn.finishedAt - turn.startedAt))} s${turn.seen ? ' (seen)' : ''}; alerts: ${follow.map(f => f.type).join(', ') || 'none'}`);
        await carryOut(follow);
      } else if (effect.type === 'label' && !asked.has(effect.id)) {
        asked.add(effect.id);
        const history = await read(`/api/history/${encodeURIComponent(effect.id)}?limit=1`, 5000);
        if (history.body && history.body.name) turns.setLabel(state, effect.id, history.body.name);
      } else if (effect.type === 'notify') notify(effect);
      else if (effect.type === 'speak') void speak(effect.text);
    }
  }
  async function round() {
    if (stopped) return;
    if (busy) { again = true; return; }
    busy = true;
    let next = idleMs;
    try {
      const activity = await read('/api/chat/activity', 8000);
      if (activity.status === 401 || activity.status === 403) { link = 'signed-out'; next = idleMs * 3; return; }
      if (activity.status !== 200) { failures++; if (failures >= 3) link = 'offline'; next = Math.min(60000, idleMs * failures); return; }
      failures = 0; link = '';
      let questions = null;
      if (!state.primed || Date.now() - questionsAt >= questionsMs || turns.needsQuestions(state, activity.body)) {
        const answer = await read('/api/questions', 8000);
        if (answer.status === 200) { questions = answer.body; questionsAt = Date.now(); }
      }
      await refreshSettings(settingsMs);
      const effects = turns.observe(state, turns.snapshotFrom(activity.body, questions, Date.now() / 1000), context());
      await carryOut(effects);
      visit();
      const now = turns.summary(state);
      next = now.running || now.waiting ? liveMs : idleMs;
    } catch (error) {
      log('round failed: ' + (error && error.message));
    } finally {
      busy = false;
      draw();
      persist();
      if (again) { again = false; next = 0; }
      schedule(next);
    }
  }
  function schedule(delay) {
    clearTimeout(timer);
    if (!stopped) timer = setTimeout(() => void round(), Math.max(0, delay));
  }
  function persist() {
    if (!stateFile) return;
    const text = JSON.stringify(turns.serialize(state));
    if (text === lastSaved) return;
    try { mkdirSync(dirname(stateFile), {recursive: true}); writeFileSync(stateFile + '.tmp', text); renameSync(stateFile + '.tmp', stateFile); lastSaved = text; } catch { /* a courtesy for the next start */ }
  }
  /** The chat on screen in the focused window is seen. */
  function visit() {
    const where = focus();
    return Boolean(where.focused && where.visibleId && turns.markSeen(state, where.visibleId));
  }

  // ── the icon and its menu ─────────────────────────────────────────────────
  function image(mark) {
    const key = mark ? `${mark.kind}|${mark.glyph}|${mark.corner}` : 'idle';
    if (images.has(key)) return images.get(key);
    let out = null;
    try {
      const base = nativeImage.createFromPath(iconPath);
      if (!mark) out = base;
      else if (!base.isEmpty()) {
        out = nativeImage.createEmpty();
        for (const size of ICON_SIZES) {
          const bitmap = base.resize({width: size, height: size, quality: 'best'}).toBitmap();
          if (bitmap.length !== size * size * 4) continue;
          out.addRepresentation({scaleFactor: size / 16, width: size, height: size, buffer: badge.paint(bitmap, size, mark)});
        }
        if (out.isEmpty()) out = base;
      }
    } catch (error) { log('icon: ' + error.message); }
    images.set(key, out);
    return out;
  }
  function overlay(mark) {
    if (!mark) return null;
    const key = `over|${mark.kind}|${mark.glyph}`;
    if (!images.has(key)) {
      let out = null;
      try {
        out = nativeImage.createEmpty();
        for (const size of [16, 32]) out.addRepresentation({scaleFactor: size / 16, width: size, height: size, buffer: badge.alone(size, mark)});
      } catch { out = null; }
      images.set(key, out);
    }
    return images.get(key);
  }
  const open = id => { if (turns.markSeen(state, id)) persist(); openChat(id); draw(); };
  function draw(force) {
    if (stopped) return;
    const language = lang(), summary = turns.summary(state), tip = turns.tooltip(state, language, link), sections = turns.menu(state, language, Date.now() / 1000);
    const mark = badge.markFor(summary, link), L = turns.strings(language);
    const signature = JSON.stringify([tip, sections, mark, settings.notify, settings.voice]);
    if (!force && signature === lastDrawn) return;
    lastDrawn = signature;
    const items = [{label: tip.replace(/&/g, '&&'), enabled: false}];
    for (const section of sections) {
      items.push({type: 'separator'}, {label: section.title, enabled: false});
      for (const item of section.items) items.push({label: item.text.replace(/&/g, '&&'), click: () => open(item.id)});
    }
    const toggles = [
      {label: L.notifyToggle, type: 'checkbox', checked: settings.notify, click: item => void saveSetting('notify', Boolean(item.checked))},
      {label: L.voiceToggle, type: 'checkbox', checked: settings.voice, click: item => void saveSetting('voice', Boolean(item.checked))},
    ];
    try {
      const icon = image(mark);
      if (icon) tray.setImage(icon);
      tray.setToolTip(tip.slice(0, 127));
      rebuildMenu(items, toggles);
      setOverlay(overlay(mark), mark ? tip : '');
    } catch (error) { log('tray: ' + error.message); }
  }

  // ── the alerts ────────────────────────────────────────────────────────────
  function notify({title, body, id, kind}) {
    try {
      if (Notification.isSupported && !Notification.isSupported()) { log('notification: not supported here'); return false; }
      const toast = new Notification({title, body, icon: iconPath || undefined, silent: false});
      shown.add(toast);   // a collected toast loses its click
      toast.on('click', () => { shown.delete(toast); if (id) open(id); else showMain(); });
      toast.on('close', () => shown.delete(toast));
      toast.on('failed', (_event, error) => { shown.delete(toast); log('notification failed: ' + error); });
      toast.show();
      if (shown.size > 12) shown.delete(shown.values().next().value);
      log(`notification (${kind}): ${title} | ${body}`);
      return true;
    } catch (error) { log('notification: ' + error.message); return false; }
  }
  async function ensurePlayer() {
    if (player && !player.isDestroyed()) return player;
    player = new BrowserWindow({show: false, width: 1, height: 1, skipTaskbar: true, focusable: false,
      webPreferences: {preload: join(__dirname, 'announce-preload.cjs'), nodeIntegration: false, contextIsolation: true, sandbox: true, autoplayPolicy: 'no-user-gesture-required', backgroundThrottling: false}});
    await player.loadURL(PLAYER_PAGE);
    return player;
  }
  function closePlayer() {
    clearTimeout(playerTimer);
    if (player && !player.isDestroyed()) player.destroy();
    player = null;
  }
  const sniff = audio => audio.startsWith('UklGR') ? 'audio/wav' : audio.startsWith('T2dn') ? 'audio/ogg' : 'audio/mpeg';
  /**
   * One spoken sentence: the voice Faustus is set up with (`POST /api/tts/synthesize`), played by a hidden window of this
   * app; when that Faustus has no voice of its own (a Spark with the Windows voices selected), the voices of this PC.
   */
  async function speak(text) {
    const language = turns.strings(lang()) === turns.strings('en') ? 'en' : 'es';
    let audio = '', mime = '';
    try {
      const response = await request('/api/tts/synthesize', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text, format: 'base64', language})}, 30000);
      if (response.ok) { const body = await response.json(); audio = String(body.audio || ''); mime = body.mime || sniff(audio); }
      else log(`voice: Faustus speech answered ${response.status}; using the voices of this PC`);
    } catch (error) { log(`voice: Faustus speech failed (${error.message}); using the voices of this PC`); }
    try {
      const window = await ensurePlayer();
      if (window.isDestroyed()) return false;
      window.webContents.send('faustus:announce', {audio, mime, text, lang: language});
      clearTimeout(playerTimer);
      playerTimer = setTimeout(closePlayer, 120000);
      log(`voice: sent to play (${audio ? `Faustus speech, ${Math.round(audio.length * 0.75 / 1024)} KB ${mime}` : 'voices of this PC'}): ${text}`);
      return true;
    } catch (error) { log('voice: ' + error.message); return false; }
  }
  function voiceResult(_event, result) {
    lastVoice = {...result, at: Date.now()};
    log('voice: ' + JSON.stringify(result));
    if (result && (result.started || result.ended || result.error)) { for (const waiter of voiceWaiters) waiter(result); voiceWaiters.clear(); }
  }
  ipcMain.on('faustus:announce-result', voiceResult);

  /** The "Send a test alert" button of Settings: the toast and the spoken line, whatever the switches say. */
  async function test() {
    const L = turns.strings(lang());
    const notified = notify({title: L.testTitle, body: L.testBody, id: '', kind: 'test'});
    const heard = new Promise(resolve => { voiceWaiters.add(resolve); setTimeout(() => { voiceWaiters.delete(resolve); resolve({error: 'timeout'}); }, 40000); });
    const sent = await speak(L.testSay);
    const voice = sent ? await heard : {error: 'not sent'};
    return {notified, voice: {ok: Boolean(voice && !voice.error), via: (voice && voice.via) || '', error: (voice && voice.error) || ''}};
  }

  draw(true);
  schedule(0);
  return {
    stop() { stopped = true; clearTimeout(timer); closePlayer(); ipcMain.removeListener('faustus:announce-result', voiceResult); persist(); },
    /** Look now: the window just sent a message or a stream just closed. */
    kick(delay = 0) { if (busy) again = true; else schedule(delay); },
    /** The window got the focus or moved to another chat. */
    touch() { if (visit()) { persist(); draw(); } },
    /** The address changed (Faustus moved between this PC and a Spark): forget the live turns of the old one. */
    retarget() { for (const turn of [...state.turns.values()]) if (!['done', 'failed', 'stopped'].includes(turn.state)) state.turns.delete(turn.id); state.primed = false; failures = 0; link = ''; settingsAt = 0; questionsAt = 0; draw(true); schedule(0); },
    test,
    view() { return {summary: turns.summary(state), tooltip: turns.tooltip(state, lang(), link), link, settings, voice: lastVoice}; },
    state,
  };
}
module.exports = {startTurnTray};
