'use strict';
const {join} = require('node:path');
const {readFileSync, writeFileSync} = require('node:fs');

// The server owns the preference, including when it lives on a Spark. Only
// the desktop owns the window and global accelerator. Hiding keeps the real
// Studio composer, uploads and running stream alive.
function startQuickQuestion({BrowserWindow, globalShortcut, ipcMain, net, screen}, {
  origin, profile, secureWindow = () => {}, openChat, showMain, pollMs = 3000, logger = console,
} = {}) {
  let window = null, registered = '', error = '', stopped = false, polling = false;
  const stateFile = join(profile, 'quick-question.json');
  let handled = '';
  try { handled = JSON.parse(readFileSync(stateFile, 'utf8')).handled || ''; } catch { /* first launch */ }
  const alive = () => window && !window.isDestroyed();
  const base = () => typeof origin === 'function' ? origin() : origin;
  const view = () => ({registered, error, visible: Boolean(alive() && window.isVisible())});
  function show(sessionId) {
    if (stopped) return;
    const target = base() + '/studio?quick=1' + (sessionId ? '&s=' + encodeURIComponent(sessionId) : '');
    if (!alive()) {
      const area = screen.getDisplayNearestPoint(screen.getCursorScreenPoint()).workArea;
      const width = Math.min(620, area.width), height = Math.min(680, area.height);
      window = new BrowserWindow({title: 'Faustus', width, height, minWidth: 390, minHeight: 420,
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
      created.webContents.on('did-finish-load', () => { if (!stopped && !created.isDestroyed()) { created.show(); created.focus(); } });
      void created.loadURL(target).catch(err => { error = err.message; logger.warn('Quick question:', error); });
    } else {
      if (sessionId) void window.loadURL(target).catch(err => { error = err.message; });
      window.show(); window.focus();
      window.webContents.send('faustus:quick-focus');
    }
  }
  function trusted(event) {
    if (!alive() || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) return false;
    try { return new URL(event.senderFrame.url).origin === base(); } catch { return false; }
  }
  ipcMain.handle('faustus:quick', (event, action, id) => {
    if (!trusted(event)) throw new Error('Untrusted quick question window');
    if (action === 'studio') {
      if (typeof id === 'string' && id) openChat(id); else showMain();
      window.hide();
    } else if (action === 'hide') window.hide();
    else if (action !== 'state') throw new Error('Unknown quick question action');
    return view();
  });
  async function poll() {
    if (stopped || polling) return;
    polling = true;
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await net.fetch(base() + '/api/auth/settings', {signal: controller.signal});
      if (!response.ok || stopped) return;
      const settings = await response.json();
      if (stopped) return;
      const wanted = settings.quick_question_enabled === false ? '' : String(settings.quick_question_hotkey || 'Ctrl+Shift+Space').trim();
      if (wanted !== registered) {
        // Reserve the replacement before releasing the working key. A typo
        // or another app using it leaves the previous shortcut available.
        let ok = !wanted;
        try { if (wanted) ok = globalShortcut.register(wanted, () => show()); } catch { /* invalid accelerator */ }
        if (ok) {
          if (registered) globalShortcut.unregister(registered);
          registered = wanted; error = '';
        } else error = 'Shortcut unavailable: ' + wanted;
      }
      const request = settings.quick_question_request;
      if (request && typeof request.id === 'string' && request.id !== handled) {
        show(typeof request.session_id === 'string' ? request.session_id : '');
        handled = request.id;
        try { writeFileSync(stateFile, JSON.stringify({handled})); } catch (err) { logger.warn('Quick question:', err.message); }
      }
    } catch (err) { if (!stopped) logger.warn('Quick question settings:', err.message); }
    finally { clearTimeout(timer); polling = false; }
  }
  const timer = setInterval(() => void poll(), pollMs); timer.unref?.();
  void poll();
  return {show, view, poll,
    retarget() { if (alive()) window.destroy(); window = null; void poll(); },
    stop() { stopped = true; clearInterval(timer); if (registered) globalShortcut.unregister(registered);
      registered = ''; ipcMain.removeHandler('faustus:quick'); if (alive()) window.destroy(); window = null; },
  };
}
module.exports = {startQuickQuestion};
