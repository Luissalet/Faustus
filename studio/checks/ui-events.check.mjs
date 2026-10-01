// What the agent's ui_control asks the screen to do reaches Studio and is
// applied (shell/uiEvents.ts): a theme by name, a theme the agent made, the
// panel routes and the reply draft. Two built-in palettes were renamed on
// 01-10-2026; a theme saved with one of them keeps its look and its tick.
//
// Run by tests/test_ui_events_js.py, or by hand:
//   node studio/checks/ui-events.check.mjs
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const tmp = mkdtempSync(join(tmpdir(), 'fs-ui-events-'));
const entry = join(tmp, 'entry.ts');
const src = (p) => JSON.stringify(join(root, 'studio/src', p).replace(/\\/g, '/'));
writeFileSync(entry, [
  `export * from ${src('shell/uiEvents.ts')};`,
  `export { getTheme, getCustomThemes, PRESETS } from ${src('shell/appearance.ts')};`,
  `export { uiEventFrom } from ${src('adapters/uiEvent.ts')};`,
  `export { decode } from ${src('adapters/chat.ts')};`,
].join('\n'));
const out = join(tmp, 'ui-events.mjs');
await build({ entryPoints: [entry], bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent', jsx: 'automatic', loader: { '.css': 'empty', '.svg': 'empty', '.png': 'empty' } });

const window = new Window({ url: 'https://studio.faustus.invalid/' });
for (const key of ['window', 'document', 'HTMLElement', 'Element', 'Node', 'localStorage', 'sessionStorage', 'getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame', 'Response', 'Headers', 'CustomEvent', 'Event']) {
  if (key in window) {
    try { globalThis[key] = window[key]; } catch { /* getter-only in some Node versions */ }
  }
}
// Background effects draw on a canvas happy-dom does not paint: a context that accepts everything.
const noop = new Proxy(function () {}, { get: (_t, k) => (k === Symbol.toPrimitive ? () => 0 : noop), apply: () => noop, set: () => true });
window.HTMLCanvasElement.prototype.getContext = () => noop;
const terracotta = { bg: '#262624', fg: '#f5f4f0', panel: '#30302e', border: '#4a4a47', red: '#c6613f' };
const graphite = { bg: '#212121', fg: '#ececec', panel: '#171717', border: '#424242', red: '#949494' };
const calls = [];
globalThis.fetch = async (url, init = {}) => {
  const u = String(url);
  calls.push({ url: u, method: init.method || 'GET', body: init.body });
  if (u.includes('/api/email/read/')) {
    return new Response(JSON.stringify({ uid: '42', subject: 'Cena del viernes', from_name: 'Ana Ruiz', from_address: 'ana@example.com', to: 'yo@example.com', cc: 'leo@example.com', message_id: '<m42@example.com>', references: '', body: 'Hola' }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }
  return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
};

// A theme saved under the palette's old name, before the module loads.
localStorage.setItem('odysseus-theme', JSON.stringify({ name: 'old-palette-name', colors: terracotta }));
const m = await import(pathToFileURL(out).href);

assert.ok(m.PRESETS.graphite && m.PRESETS.terracotta, 'renamed presets exist');
assert.deepEqual(m.PRESETS.terracotta, terracotta);
assert.deepEqual(m.PRESETS.graphite, graphite);
assert.equal(m.getTheme().name, 'terracotta', 'a theme saved with a renamed preset keeps its tick');
assert.deepEqual(m.getTheme().colors, terracotta);

// decode: the tool_output event carries the ui_event
const ev = m.decode({ type: 'tool_output', tool: 'ui_control', command: 'set_theme forest', output: "Theme changed to 'forest'", ui_event: 'set_theme', theme_name: 'forest' }, null);
assert.equal(ev.type, 'tool_output');
assert.deepEqual({ kind: ev.uiEvent.kind, themeName: ev.uiEvent.themeName }, { kind: 'set_theme', themeName: 'forest' });
const plain = m.decode({ type: 'tool_output', tool: 'bash', command: 'ls', output: '' }, null);
assert.equal(plain.uiEvent, undefined);

// set_theme: preset, case-insensitive; unknown is nothing
assert.deepEqual(m.applyThemeEvent({ kind: 'set_theme', themeName: 'Forest' }), { name: 'forest', saved: true });
assert.equal(m.getTheme().name, 'forest');
assert.equal(document.documentElement.style.getPropertyValue('--bg'), '#1b2a1b');
assert.equal(m.applyThemeEvent({ kind: 'set_theme', themeName: 'no-such-theme' }), null);
assert.equal(m.getTheme().name, 'forest');
assert.equal(m.applyThemeEvent({ kind: 'set_theme', themeName: 'studio' }).name, 'Studio');
assert.equal(document.documentElement.style.getPropertyValue('--bg'), '');

// create_theme: applied and kept among the user's themes, with its effect
const made = m.applyThemeEvent({ kind: 'create_theme', themeName: 'volcan', colors: { bg: '#1a0f0a', fg: '#ffd7b0', panel: '#24140c', border: '#5a2a14', red: '#ff5a1f', advanced: { codeBg: '#000000' } }, bg: { pattern: 'embers', effectColor: '#ff5a1f', effectIntensity: 1.5 } });
assert.deepEqual(made, { name: 'volcan', saved: true });
assert.equal(m.getTheme().name, 'volcan');
assert.equal(m.getTheme().bgPattern, 'embers');
assert.equal(m.getTheme().bgEffectIntensity, 1.5);
assert.ok(m.getCustomThemes().volcan, 'kept among the user themes');
assert.ok(calls.some((c) => c.method === 'PUT' && c.url.endsWith('/api/prefs/custom-themes')));
assert.equal(m.applyThemeEvent({ kind: 'create_theme', themeName: 'roto', colors: { bg: 'red' } }), null);
// the user's own theme by name
m.applyThemeEvent({ kind: 'set_theme', themeName: 'forest' });
assert.equal(m.applyThemeEvent({ kind: 'set_theme', themeName: 'VOLCAN' }).name, 'volcan');

assert.equal(m.applyThemeEvent({ kind: 'set_theme', themeName: 'terminal' }).name, 'terminal'); // perlin-flow
assert.equal(m.applyThemeEvent({ kind: 'set_theme', themeName: 'retrowave' }).name, 'retrowave'); // embers
m.applyThemeEvent({ kind: 'set_theme', themeName: 'volcan' });
// a user theme that happens to carry a preset's colours keeps its own name
assert.equal(m.themeNamed('volcan').name, 'volcan');

// open_panel targets are real Studio routes
assert.equal(m.PANEL_ROUTES.notes, '/notes');
assert.equal(m.PANEL_ROUTES.email, '/email');
assert.equal(m.PANEL_ROUTES.documents, '/library?type=documento');
assert.equal(m.PANEL_ROUTES.cookbook, '/cookbook');

// open_email_reply: the reply draft is left for the mail screen
const to = await m.emailReplyRoute({ kind: 'open_email_reply', uid: '42', folder: 'INBOX', mode: 'reply-all', body: 'Me apunto.' });
assert.equal(to, '/email?compose=handoff');
const draft = JSON.parse(sessionStorage.getItem('fs-compose-handoff'));
assert.equal(draft.to, 'Ana Ruiz <ana@example.com>');
assert.equal(draft.subject, 'Re: Cena del viernes');
assert.equal(draft.body, 'Me apunto.');
assert.equal(draft.inReplyTo, '<m42@example.com>');
assert.ok(draft.cc.includes('leo@example.com'));
assert.equal(await m.emailReplyRoute({ kind: 'open_email_reply' }), '/email');

// highlight / clear
document.body.innerHTML = '<button id="send">Send</button>';
assert.equal(m.highlight('#send', 'Here'), 1);
assert.ok(document.querySelector('#send').classList.contains('fs-ui-highlight'));
assert.equal(m.highlight('##bad', 'x'), 0);
m.clearHighlights();
assert.ok(!document.querySelector('#send').classList.contains('fs-ui-highlight'));

console.log('ALL OK ui-events');
process.exit(0); // the background effect keeps animating
