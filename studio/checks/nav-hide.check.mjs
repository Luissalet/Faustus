import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { Window } from 'happy-dom';

/* The sidebar can be put away and brought back: the state and what is stored
   (shell/navSize.ts), the button drawn on the sidebar and where the sidebar
   was (shell/navToggle.tsx), and the other ways to it — the shortcut, the
   palette and the agent's switch. */

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const temporary = mkdtempSync(join(tmpdir(), 'faustus-nav-hide-'));
const browser = new Window({ url: 'http://127.0.0.1/' });
for (const key of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'MutationObserver', 'localStorage', 'requestAnimationFrame']) {
  Object.defineProperty(globalThis, key, { configurable: true, value: typeof browser[key] === 'function' && key === 'requestAnimationFrame' ? browser[key].bind(browser) : browser[key] });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ keybinds: { toggle_nav: 'ctrl+shift+l' } }) });
const KEY = 'faustus_studio_nav';

try {
  const outfile = join(temporary, 'test.mjs');
  await build({ stdin: { contents: `
    export * from './studio/src/shell/navSize';
    export * from './studio/src/shell/navToggle';
    export { DEFAULT_KEYBINDS, KEYBIND_LABELS, matchesCombo } from './studio/src/adapters/settings';
    export { createElement, act } from 'react';
    export { createRoot } from 'react-dom/client';
  `, resolveDir: root, loader: 'tsx' }, bundle: true, platform: 'node', format: 'esm', outfile, loader: { '.css': 'empty' }, logLevel: 'silent' });

  // A choice made before this existed still reads; junk is the default sidebar.
  window.localStorage.setItem(KEY, JSON.stringify({ mode: 'rail', width: 300 }));
  const m = await import(pathToFileURL(outfile));
  assert.deepEqual(m.parseNav(null), { mode: 'auto', width: 236, back: 'auto' });
  assert.deepEqual(m.parseNav('{nope'), { mode: 'auto', width: 236, back: 'auto' });
  assert.deepEqual(m.parseNav('{"mode":"wide","width":9000}'), { mode: 'wide', width: 236, back: 'wide' });
  assert.deepEqual(m.parseNav('{"mode":"hidden","width":300}'), { mode: 'hidden', width: 300, back: 'auto' }, 'hidden with nothing to go back to goes back to the default');
  assert.deepEqual(m.parseNav('{"mode":"hidden","width":300,"back":"hidden"}').back, 'auto');

  // Hiding remembers how it stood; showing puts that back; both are idempotent.
  const rail = m.parseNav(JSON.stringify({ mode: 'rail', width: 300 }));
  const hidden = m.hideNav(rail);
  assert.deepEqual(hidden, { mode: 'hidden', width: 300, back: 'rail' });
  assert.equal(m.hideNav(hidden), hidden);
  assert.deepEqual(m.showNav(hidden), { mode: 'rail', width: 300, back: 'rail' });
  assert.equal(m.showNav(rail), rail);

  // The one state everything moves, and what is left in storage.
  assert.equal(m.navHidden(), false);
  m.setNavHidden(true);
  assert.equal(m.navHidden(), true);
  assert.deepEqual(JSON.parse(window.localStorage.getItem(KEY)), { mode: 'hidden', width: 300, back: 'rail' });
  m.setNavHidden(true);
  assert.equal(m.navHidden(), true, 'hiding twice is still hidden');
  m.toggleNavHidden();
  assert.equal(m.navHidden(), false);
  assert.deepEqual(JSON.parse(window.localStorage.getItem(KEY)), { mode: 'rail', width: 300, back: 'rail' }, 'it comes back as the rail it was');

  // Looking out over the page: only while put away, never stored, and over when the sidebar comes back.
  assert.equal(m.navPeeking(), false);
  m.setNavPeek(true);
  assert.equal(m.navPeeking(), false, 'a sidebar that is there has nothing to look out from');
  m.setNavHidden(true);
  m.setNavPeek(true);
  assert.equal(m.navPeeking(), true);
  assert.equal(JSON.parse(window.localStorage.getItem(KEY)).mode, 'hidden', 'looking out is not a choice that is stored');
  m.setNavPeek(false);
  assert.equal(m.navPeeking(), false);
  m.setNavPeek(true);
  m.setNavHidden(false);
  assert.equal(m.navPeeking(), false, 'bringing it back ends the look');
  m.setNavHidden(true);
  assert.equal(m.navPeeking(), false, 'and putting it away again does not start one');
  m.setNavHidden(false);

  // The button: says what it will do, carries the person's own shortcut, and moves the state.
  const host = document.createElement('div');
  document.body.appendChild(host);
  const rootNode = m.createRoot(host);
  await m.act(async () => { rootNode.render(m.createElement(m.NavToggle)); });
  await m.act(async () => { await new Promise((done) => setTimeout(done, 20)); });
  const button = host.querySelector('button.fs-nav__toggle');
  assert.ok(button, 'the button is drawn');
  assert.equal(button.getAttribute('aria-label'), 'Hide the sidebar');
  assert.equal(button.getAttribute('aria-expanded'), 'true');
  assert.match(button.getAttribute('title'), /Ctrl\+Shift\+L/, 'the tooltip names the shortcut as configured');
  await m.act(async () => { button.click(); });
  assert.equal(m.navHidden(), true, 'a click puts it away');
  assert.equal(button.getAttribute('aria-label'), 'Show the sidebar');
  assert.equal(button.getAttribute('aria-expanded'), 'false');
  await m.act(async () => { button.click(); });
  assert.equal(m.navHidden(), false, 'and a click brings it back');
  await m.act(async () => { rootNode.unmount(); });

  // The shortcut sits with the others, with a default nobody else uses.
  assert.equal(m.DEFAULT_KEYBINDS.toggle_nav, 'ctrl+.');
  assert.ok(m.KEYBIND_LABELS.toggle_nav, 'it has a name in Settings');
  assert.notEqual(m.KEYBIND_LABELS.toggle_nav, m.KEYBIND_LABELS.toggle_sidebar, 'and the conversations pane keeps a name of its own');
  const used = Object.entries(m.DEFAULT_KEYBINDS).filter(([, combo]) => combo === 'ctrl+.');
  assert.equal(used.length, 1);
  assert.equal(m.comboLabel('ctrl+.'), 'Ctrl+.');

  // Wiring: on the sidebar, where the sidebar was, on the window's bar, in the palette, on the key and for the agent.
  const read = (path) => readFileSync(join(root, path), 'utf8');
  const shell = read('studio/src/shell/AppShell.tsx');
  assert.ok(shell.includes("{platform !== 'mobile' && <NavToggle />}"), 'the sidebar carries the button');
  assert.ok(/navMode === 'hidden' && \(\s*<div className="fs-nav-gutter"[^>]*>\s*<NavToggle \/>/.test(shell), 'and so does the place it leaves');
  assert.ok(shell.includes("platform === 'mobile' && nav.mode === 'hidden' ? 'rail'"), "the phone's tab bar is never put away");
  assert.ok(shell.includes("{navMode === 'hidden' && <NavPeek />}") && shell.includes("data-peek={(navMode === 'hidden' && peek) || undefined}"), 'put away, its edge calls it out');
  assert.ok(shell.includes('matchesCombo(event, navCombo)'), 'the shortcut works on every screen');
  assert.ok(read('studio/src/shell/DesktopBar.tsx').includes('<NavToggle className="fs-desktop-bar__nav" />'), "the desktop window's bar has it");
  assert.ok(read('studio/src/shell/CommandPalette.tsx').includes('flipNav()'), 'the palette has it');
  const studio = read('studio/src/screens/Studio.tsx');
  assert.ok(studio.includes('toggle_nav: flipNav'), 'the key also works with a conversation open');
  assert.ok(studio.includes("name === 'sidebar'") && studio.includes('setNavHidden(!on)'), "the agent's switch reaches the sidebar");
  const css = read('studio/src/styles/shell.css');
  assert.ok(css.includes(".fs-shell[data-nav='hidden'] > .fs-nav,"), 'hidden means not drawn');
  assert.ok(shell.includes('<span className="fs-nav__name"><span>Faustus</span></span>') && css.includes('.fs-nav__name::before'), 'on a narrow sidebar the name gives way to the button, whole');
  assert.ok(css.includes(".fs-shell[data-nav='hidden'][data-peek] > .fs-nav {") && css.includes('.fs-nav-edge {'), 'looking out, it is drawn over the page');
  for (const key of ['Hide the sidebar', 'Show the sidebar', 'Keep the sidebar open', 'Show or hide the conversations']) {
    assert.ok(read('docs/ui/i18n/es.tsv').split('\n').some((line) => line.startsWith(key + '\t')), 'Spanish for: ' + key);
  }
  console.log('ok nav-hide');
} finally {
  await browser.happyDOM.abort();
  rmSync(temporary, { recursive: true, force: true });
}
process.exit(0);
