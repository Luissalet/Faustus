// The side panel's state (studio/src/screens/studio/panel.ts) and the frame
// whitelist it is fed through (adapters/chat.ts `safeFrameSrc`).
//
// What the agent sees while it works: a viewport frame after each browser
// action, and a screenshot after a desktop one. Three things here were
// bugs once — a frame source that is not a raster (an SVG or an HTML data
// URL is a script), an unbounded list of frames eating memory over a long
// session, and a panel that opened itself over the user's work every time
// instead of once.
//
// Bundled with esbuild on the fly; run by tests/test_studio_panel_js.py, or
// by hand:
//   node studio/checks/panel.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-panel-'));
async function load(rel, name) {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', rel)], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

// localStorage: the panel reads the auto-open preference from it.
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

const p = await load(join('screens', 'studio', 'panel.ts'), 'panel.mjs');
const chat = await load(join('adapters', 'chat.ts'), 'chat.mjs');

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

const PNG = 'data:image/png;base64,iVBORw0KGgo=';
const frameOf = (over = {}) => ({ src: PNG, url: 'https://example.com/', title: 'Example', tool: 'browser_navigate', source: 'browser', at: 0, ...over });
const feed = (state, event, busy = true) => p.panelReducer(state, { type: 'event', event, busy });

// ── What is allowed to be a frame ──
{
  assert(chat.safeFrameSrc(PNG) === PNG, 'a base64 PNG is a frame');
  assert(chat.safeFrameSrc('data:image/jpeg;base64,/9j/4AAQ') !== '', 'so is a JPEG');
  assert(chat.safeFrameSrc('data:image/webp;base64,UklGRg==') !== '', 'and a WebP');
  assert(chat.safeFrameSrc('data:image/svg+xml;base64,PHN2Zz4=') === '', 'an SVG is not: it can carry script');
  assert(chat.safeFrameSrc('data:text/html;base64,PGI+') === '', 'nor is an HTML data URL');
  assert(chat.safeFrameSrc('https://example.com/x.png') === '', 'nor a remote URL: frames are inline or nothing');
  assert(chat.safeFrameSrc('javascript:alert(1)') === '', 'and certainly not javascript:');
  assert(chat.safeFrameSrc(null) === '' && chat.safeFrameSrc(undefined) === '', 'nothing is nothing');
}

// ── The frame list is bounded ──
{
  let s = p.initialPanel;
  for (let i = 0; i < p.MAX_FRAMES + 5; i++) s = feed(s, { type: 'frame', frame: frameOf({ title: `f${i}` }) });
  assert(s.frames.length === p.MAX_FRAMES, `a long session keeps ${p.MAX_FRAMES} frames, not all of them`);
  assert(s.frames[s.frames.length - 1].title === `f${p.MAX_FRAMES + 4}`, 'and the newest is the one kept');
  assert(s.frames[0].title === 'f5', 'the oldest are the ones dropped');
  assert(s.active === s.frames.length - 1, 'the newest frame is the one shown');
}

// ── It opens itself once, not every time ──
{
  store.clear();
  let s = feed(p.initialPanel, { type: 'frame', frame: frameOf() });
  assert(s.open && s.tab === 'browser', 'the first frame of a turn opens the panel on the browser tab');

  s = p.panelReducer(s, { type: 'close' });
  s = feed(s, { type: 'frame', frame: frameOf() });
  assert(!s.open, 'closed mid-turn, it stays closed: the user just said no');

  s = p.panelReducer(s, { type: 'turn-end' });
  s = p.panelReducer(s, { type: 'turn-start' });
  s = feed(s, { type: 'frame', frame: frameOf() });
  assert(s.open, 'a new turn may open it again');

  p.setAutoOpen(false);
  let off = p.panelReducer(p.initialPanel, { type: 'turn-start' });
  off = feed(off, { type: 'frame', frame: frameOf() });
  assert(!off.open, 'with auto-open off it never opens itself');
  assert(off.frames.length === 1, 'but the frame is still collected, so the tab has it when opened');
  p.setAutoOpen(true);
  assert(p.autoOpenEnabled(), 'the preference round-trips');
  store.clear();
  assert(p.autoOpenEnabled(), 'and defaults to on when nothing is stored');
}

// ── Live means a browser action in the turn that is streaming ──
{
  let s = p.panelReducer(p.initialPanel, { type: 'turn-start' });
  assert(!s.live, 'a turn starts not live');
  s = feed(s, { type: 'frame', frame: frameOf() }, true);
  assert(s.live, 'a frame while streaming is live');
  s = p.panelReducer(s, { type: 'turn-end' });
  assert(!s.live, 'and the end of the turn ends it');
  const idle = feed(p.panelReducer(p.initialPanel, { type: 'turn-start' }), { type: 'frame', frame: frameOf() }, false);
  assert(!idle.live, 'a frame arriving with nothing streaming is not live');
}

// ── A desktop screenshot shares the panel, labelled as itself ──
{
  const s = feed(p.initialPanel, { type: 'tool_output', tool: 'desktop_screenshot', screenshot: PNG, text: '' });
  assert(s.frames.length === 1, 'a desktop screenshot becomes a frame');
  assert(s.frames[0].source === 'desktop', 'marked as the desktop, not the browser');
  assert(s.frames[0].title, 'and it gets a name of its own rather than an empty caption');

  const browser = feed(p.initialPanel, { type: 'tool_output', tool: 'browser_take_screenshot', screenshot: PNG, text: '' });
  assert(browser.frames[0].source === 'browser', 'a browser tool stays a browser frame');

  const none = feed(p.initialPanel, { type: 'tool_output', tool: 'read_file', text: 'x' });
  assert(none.frames.length === 0, 'a tool with no screenshot adds nothing');
}

// ── Showing a frame by index ──
{
  let s = p.initialPanel;
  for (let i = 0; i < 3; i++) s = feed(s, { type: 'frame', frame: frameOf({ title: `f${i}` }) });
  assert(p.panelReducer(s, { type: 'show', index: 0 }).active === 0, 'an earlier frame can be brought back');
  assert(p.panelReducer(s, { type: 'show', index: 99 }).active === s.active, 'an index that is not there changes nothing');
  assert(p.panelReducer(s, { type: 'show', index: -1 }).active === s.active, 'and neither does a negative one');
}

// ── Switching session clears what belonged to the last one ──
{
  let s = feed(p.initialPanel, { type: 'frame', frame: frameOf() });
  s = p.panelReducer(s, { type: 'file', workspace: 'w', path: 'a.py' });
  s = p.panelReducer(s, { type: 'session-switch' });
  assert(s.file === null && s.doc === null && !s.live, 'the document, the file and live all belong to the session that left');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
