// desktop/app-shell.cjs — F1.4 "Apps" wave, lot D: a generic desktop shell for
// ANY launch-profile app, not just Faustus itself. One Electron window per
// app, invoked as:
//
//   electron app-shell.cjs --url=<http://127.0.0.1:port/...> --title=<name> \
//     --icon=<abs path png/ico> --slug=<profile id>
//
// `src/launch_profiles.py::open_desktop()` is the only caller (spawned
// detached, same as every other launch profile) — it resolves the electron
// binary and builds this argv; this file never talks back to Faustus's own
// API, it only shows `--url` in a window.
//
// Kept in the same house style as `desktop/main.cjs` (data: URL splash,
// `policy.cjs`'s `localNavigation`/`externalNavigation` for the same
// same-origin-stays / anything-else-goes-external rule, per-checkout
// `userData` under `data/`) but deliberately dumber: no server_runtime IPC,
// no "does closing this stop a server" dialog (closing this window NEVER
// stops the app behind it — the app is somebody else's process, started and
// stopped through its own launch profile, not through this window), no
// window-open handler beyond the same navigation policy.
//
// Split in two so `node --test app-shell.test.cjs` can exercise the pure
// helpers below WITHOUT Electron installed (the container this lands in has
// no `desktop/node_modules/electron`): everything above the
// `require.main === module` guard is plain Node and has no `require('electron')`
// at module scope; everything below it only runs when this file is launched
// directly as an Electron main script (which is the only way `require.main
// === module` can be true for it — imported for its exports, as the test
// does, main is always the test file instead).
'use strict';
const {join, resolve} = require('node:path');
const {existsSync, mkdirSync, readFileSync, writeFileSync} = require('node:fs');
const {localNavigation, externalNavigation} = require('./policy.cjs');

const root = resolve(__dirname, '..');

// ── pure helpers (tested directly, no Electron needed) ─────────────────────

/** `--key=value` argv (after the electron binary + script path) → {url,
 * title, icon, slug}. Unknown flags are ignored; `--foo` with no `=value`
 * is ignored too (never treated as a boolean toggle — every flag this shell
 * reads is a string). */
function parseArgs(argv) {
  const out = {url: null, title: 'App', icon: null, slug: null};
  for (const arg of argv || []) {
    const m = /^--([a-zA-Z][a-zA-Z0-9_-]*)=([\s\S]*)$/.exec(arg);
    if (!m) continue;
    const [, key, value] = m;
    if (key === 'url') out.url = value;
    else if (key === 'title') out.title = value || out.title;
    else if (key === 'icon') out.icon = value;
    else if (key === 'slug') out.slug = value;
  }
  return out;
}

/** Every profile gets its own directory under `data/`, named by its launch
 * profile id (slug) — never shared with Faustus's own `data/desktop-profile`
 * or between two apps, so one app's cookies/window state can never leak
 * into another's. */
function profileDir(slug) {
  return join(root, 'data', 'desktop-profile-apps', String(slug));
}

function windowStateFile(slug) {
  return join(profileDir(slug), 'window.json');
}

const DEFAULT_BOUNDS = {width: 1280, height: 860};

/** Saved bounds/maximized state for `slug`, or the 1280×860 default when
 * there is none yet or the file is unreadable/corrupt — this never throws,
 * a missing or garbled window.json must not stop the app from opening. */
function loadWindowState(slug) {
  try {
    const raw = readFileSync(windowStateFile(slug), 'utf-8');
    const parsed = JSON.parse(raw);
    const state = {...DEFAULT_BOUNDS};
    if (Number.isFinite(parsed.width) && parsed.width >= 320) state.width = Math.floor(parsed.width);
    if (Number.isFinite(parsed.height) && parsed.height >= 240) state.height = Math.floor(parsed.height);
    if (Number.isFinite(parsed.x)) state.x = Math.floor(parsed.x);
    if (Number.isFinite(parsed.y)) state.y = Math.floor(parsed.y);
    if (parsed.maximized) state.maximized = true;
    return state;
  } catch {
    return {...DEFAULT_BOUNDS};
  }
}

/** Best-effort mirror of the window's current bounds/maximized flag to disk
 * — a courtesy for next launch, never load-bearing, so failures are
 * swallowed the same way `src/launch_profiles.py::_persist_own_launches`
 * treats its own mirror to `launch_state.json`. */
function saveWindowState(slug, state) {
  try {
    mkdirSync(profileDir(slug), {recursive: true});
    const {width, height, x, y, maximized} = state || {};
    writeFileSync(windowStateFile(slug), JSON.stringify({width, height, x, y, maximized: !!maximized}));
  } catch {
    /* best effort */
  }
}

/** The origin a `--url` targets, or null when it does not parse — same-origin
 * navigation inside that origin stays in the window; anything else goes to
 * `shell.openExternal` (decided with `localNavigation`/`externalNavigation`
 * from `policy.cjs`, which this module reuses rather than re-implementing). */
function targetOrigin(url) {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

/** A tiny inline splash (data: URL, CSP-locked, no external resources —
 * same shape as `desktop/main.cjs`'s own `splash`) shown while the app's
 * server is still starting. */
function splashHtml(title) {
  const safeTitle = String(title || 'App').replace(/[<>&]/g, c => ({'<': '&lt;', '>': '&gt;', '&': '&amp;'}[c]));
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(
    `<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">` +
    `<title>${safeTitle}</title>` +
    `<body style="background:#17191d;color:#eee;font:16px system-ui;margin:0;display:flex;align-items:center;justify-content:center;height:100vh">` +
    `<div style="text-align:center"><h1 style="font-weight:600">${safeTitle}</h1>` +
    `<p>Starting… / Iniciando…</p></div></body>`,
  );
}

/** Decide what a navigation/window-open target should do: `'stay'` (load it
 * in this window — same origin as `url`), `'external'` (hand it to the OS
 * browser) or `'block'` (neither — an unrecognised scheme such as
 * `javascript:` or `file:`). Exported so the test can cover the decision
 * table without a real BrowserWindow. */
function navigationDecision(target, origin) {
  if (localNavigation(target, origin)) return 'stay';
  if (externalNavigation(target)) return 'external';
  return 'block';
}

module.exports = {
  parseArgs, profileDir, windowStateFile, loadWindowState, saveWindowState,
  targetOrigin, splashHtml, navigationDecision, DEFAULT_BOUNDS, root,
};

// ── Electron entry point — only runs when launched as the main script ──────
if (require.main === module) {
  const {app, BrowserWindow, Menu, shell, net} = require('electron');

  const args = parseArgs(process.argv.slice(2));
  if (!args.url || !args.slug) {
    console.error('app-shell: --url and --slug are required');
    app.exit(1);
  } else {
    const origin = targetOrigin(args.url);
    if (!origin) {
      console.error('app-shell: --url is not a valid http(s) URL');
      app.exit(1);
    } else {
      Menu.setApplicationMenu(null);
      app.setName(args.title);
      if (process.platform === 'win32') {
        app.setAppUserModelId('faustus.app.' + args.slug);
      }
      const profile = profileDir(args.slug);
      mkdirSync(profile, {recursive: true});
      app.setPath('userData', profile);
      app.setPath('sessionData', profile);

      let win = null;
      let saveTimer = null;
      const scheduleSave = () => {
        if (!win || win.isDestroyed()) return;
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          const maximized = win.isMaximized();
          const bounds = maximized ? win.getNormalBounds() : win.getBounds();
          saveWindowState(args.slug, {...bounds, maximized});
        }, 400);
      };

      const secureNavigation = w => {
        w.webContents.setWindowOpenHandler(({url}) => {
          const decision = navigationDecision(url, origin);
          if (decision === 'stay') return {action: 'allow'};
          if (decision === 'external') void shell.openExternal(url);
          return {action: 'deny'};
        });
        w.webContents.on('will-navigate', (event, url) => {
          const decision = navigationDecision(url, origin);
          if (decision === 'stay') return;
          event.preventDefault();
          if (decision === 'external') void shell.openExternal(url);
        });
        w.webContents.on('before-input-event', (event, input) => {
          if (input.type !== 'keyDown') return;
          if (input.key === 'F12') {
            event.preventDefault();
            w.webContents.toggleDevTools();
          } else if (input.key.toLowerCase() === 'r' && (input.control || input.meta) && !input.alt) {
            event.preventDefault();
            w.webContents.reload();
          }
        });
      };

      /** Poll `url` every second for up to `timeoutMs` (default 60s) — the
       * server behind a freshly-spawned launch profile may still be coming
       * up when this shell starts, and the splash above stays on screen
       * meanwhile instead of the window loading a connection-refused page. */
      async function waitForUrl(url, timeoutMs = 60000) {
        const deadline = Date.now() + timeoutMs;
        for (;;) {
          try {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 2000);
            const response = await net.fetch(url, {signal: controller.signal});
            clearTimeout(timer);
            if (response.status < 500) return true;
          } catch {
            /* not up yet */
          }
          if (Date.now() >= deadline) return false;
          await new Promise(r => setTimeout(r, 1000));
        }
      }

      app.whenReady().then(async () => {
        const state = loadWindowState(args.slug);
        win = new BrowserWindow({
          title: args.title,
          width: state.width,
          height: state.height,
          x: state.x,
          y: state.y,
          icon: args.icon && existsSync(args.icon) ? args.icon : undefined,
          autoHideMenuBar: true,
          backgroundColor: '#17191d',
          webPreferences: {nodeIntegration: false, contextIsolation: true, sandbox: true},
        });
        if (state.maximized) win.maximize();
        secureNavigation(win);
        for (const event of ['resize', 'move', 'maximize', 'unmaximize']) win.on(event, scheduleSave);
        win.on('close', scheduleSave);
        win.webContents.on('page-title-updated', event => event.preventDefault());

        await win.loadURL(splashHtml(args.title));
        const up = await waitForUrl(args.url);
        if (win.isDestroyed()) return;
        if (!up) {
          console.error('app-shell: ' + args.url + ' did not answer within 60s');
        }
        await win.loadURL(args.url);
      });

      app.on('window-all-closed', () => app.quit());
    }
  }
}
