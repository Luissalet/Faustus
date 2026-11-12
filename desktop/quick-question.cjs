'use strict';
const {join} = require('node:path');
const {readFileSync, writeFileSync} = require('node:fs');

// The server owns the preference, including when it lives on a Spark. Only
// the desktop owns the window and global accelerator. Hiding keeps the real
// Studio composer, uploads and running stream alive.
function startQuickQuestion({BrowserWindow, globalShortcut, ipcMain, net, screen}, {
  origin, profile, secureWindow = () => {}, openChat, showMain, pollMs = 3000, logger = console,
} = {}) {
  let window = null, registered = '', shortcutUnavailable = false, stopped = false, polling = false;
  let placementRevision = 0;
  const stateFile = join(profile, 'quick-question.json');
  let handled = '';
  try { handled = JSON.parse(readFileSync(stateFile, 'utf8')).handled || ''; } catch { /* first launch */ }
  const alive = () => window && !window.isDestroyed();
  const base = () => typeof origin === 'function' ? origin() : origin;
  const view = () => ({registered, shortcutUnavailable, visible: Boolean(alive() && window.isVisible())});
  function load(window, target) {
    void window.loadURL(target).catch(err => {
      if (err.code !== 'ERR_ABORTED') logger.warn('Quick question:', err.message);
    });
  }
  function show(sessionId) {
    if (stopped) return;
    const url = new URL('/studio?quick=1', base());
    if (sessionId) url.searchParams.set('s', sessionId);
    if (!alive()) {
      const area = screen.getDisplayNearestPoint(screen.getCursorScreenPoint()).workArea;
      const width = Math.min(620, area.width), height = Math.min(680, area.height);
      window = new BrowserWindow({title: 'Faustus', width, height, minWidth: Math.min(390, width), minHeight: Math.min(420, height),
        x: Math.round(area.x + (area.width - width) / 2), y: Math.round(area.y + (area.height - height) / 3),
        frame: false, show: false, alwaysOnTop: true, skipTaskbar: true, autoHideMenuBar: true,
        backgroundColor: '#17191d', webPreferences: {preload: join(__dirname, 'quick-question-preload.cjs'),
          nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true}});
      const created = window;
      secureWindow(created);
      created.on('close', event => { if (!stopped) { event.preventDefault(); created.hide(); } });
      created.on('closed', () => { if (window === created) window = null; });
      created.webContents.on('before-input-event', (event, input) => {
        if (input.type === 'keyDown' && input.key === 'Escape') { event.preventDefault(); created.hide(); }
      });
      created.once('ready-to-show', () => { if (!stopped && !created.isDestroyed()) { created.show(); created.focus(); } });
      load(created, url.href);
    } else {
      const current = window.webContents.getURL();
      if (!current || (sessionId && new URL(current).searchParams.get('s') !== sessionId)) load(window, url.href);
      window.show(); window.focus();
      window.webContents.send('faustus:quick-focus');
    }
  }
  function trusted(event) {
    if (!alive() || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) return false;
    try {
      const url = new URL(event.senderFrame.url);
      return url.origin === base() && url.pathname === '/studio';
    } catch { return false; }
  }
  ipcMain.handle('faustus:quick', (event, action, id) => {
    if (!trusted(event)) throw new Error('Untrusted quick question window');
    if (action === 'studio') {
      window.hide();
      if (typeof id === 'string' && id) openChat(id); else showMain();
    } else if (action === 'hide') window.hide();
    else throw new Error('Unknown quick question action');
  });
  async function poll() {
    if (stopped || polling) return;
    polling = true;
    const revision = placementRevision;
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await net.fetch(base() + '/api/auth/settings', {credentials: 'include', signal: controller.signal});
      if (!response.ok) return;
      const settings = await response.json();
      if (stopped || revision !== placementRevision) return;
      const wanted = settings.quick_question_enabled === false ? '' : String(settings.quick_question_hotkey || 'Ctrl+Shift+Space').trim();
      if (wanted !== registered) {
        // Reserve the replacement before releasing the working key. A typo
        // or another app using it leaves the previous shortcut available.
        let ok = !wanted;
        try { if (wanted) ok = globalShortcut.register(wanted, () => show()); } catch { /* invalid accelerator */ }
        if (ok) {
          if (registered) globalShortcut.unregister(registered);
          registered = wanted;
        }
        shortcutUnavailable = !ok;
      } else shortcutUnavailable = false;
      const request = settings.quick_question_request;
      if (request && typeof request.id === 'string' && request.id !== handled) {
        show(typeof request.session_id === 'string' ? request.session_id : '');
        handled = request.id;
        try { writeFileSync(stateFile, JSON.stringify({handled})); } catch (err) { logger.warn('Quick question:', err.message); }
      }
    } catch (err) { if (!stopped) logger.warn('Quick question settings:', err.message); }
    finally {
      clearTimeout(timer);
      polling = false;
      if (revision !== placementRevision) void poll();
    }
  }
  const timer = setInterval(() => void poll(), pollMs); timer.unref?.();
  void poll();
  return {show, view, poll,
    retarget() {
      placementRevision++;
      if (alive()) window.destroy();
      if (registered) globalShortcut.unregister(registered);
      window = null; registered = ''; shortcutUnavailable = false;
      void poll();
    },
    stop() { stopped = true; clearInterval(timer); if (registered) globalShortcut.unregister(registered);
      registered = ''; ipcMain.removeHandler('faustus:quick'); if (alive()) window.destroy(); window = null; },
  };
}
module.exports = {startQuickQuestion};
