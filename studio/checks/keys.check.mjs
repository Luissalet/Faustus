// Keyboard shortcuts and the AltGr trap (studio/src/adapters/settings.ts).
//
// On AZERTY, QWERTZ and most non-US layouts, AltGr is how you type @ # { }
// [ ] | \ and €. The browser reports it as ctrlKey AND altKey, so without a
// guard a French user typing "@" in an email address silently fires
// Ctrl+Alt+... — which in this app is new chat, delete chat or incognito.
//
// Run by tests/test_studio_keys_js.py, or by hand:
//   node studio/checks/keys.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-keys-')), 'keys.mjs');
// The module reads `navigator.platform` at import time to decide whether it is
// on a Mac; this exercises the non-Mac path, where AltGr exists.
Object.defineProperty(globalThis, 'navigator', { value: { platform: 'Win32' }, configurable: true });
await build({ entryPoints: [join(root, 'studio', 'src', 'adapters', 'settings.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const s = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

/** A keyboard event, as the browser reports it. */
const ev = (over = {}) => ({
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  metaKey: false,
  key: 'a',
  getModifierState: () => false,
  ...over,
});
const altGraph = (over = {}) => ev({ ...over, getModifierState: (n) => n === 'AltGraph' });

// ── A real shortcut still fires ──
{
  assert(s.matchesCombo(ev({ ctrlKey: true, altKey: true, key: 'n' }), 'ctrl+alt+n'), 'ctrl+alt+n fires its shortcut');
  assert(s.matchesCombo(ev({ ctrlKey: true, key: 'k' }), 'ctrl+k'), 'ctrl+k fires');
  assert(s.matchesCombo(ev({ ctrlKey: true, shiftKey: true, key: 'p' }), 'ctrl+shift+p'), 'and a three-part combo');
  assert(s.matchesCombo(ev({ metaKey: true, key: 'k' }), 'ctrl+k'), 'cmd stands in for ctrl');
  assert(s.matchesCombo(ev({ ctrlKey: true, altKey: true, key: 'N' }), 'ctrl+alt+n'), 'the case of the key does not matter');
}

// ── AltGr never does ──
{
  assert(!s.matchesCombo(altGraph({ ctrlKey: true, altKey: true, key: '@' }), 'ctrl+alt+q'), 'AltGr typing @ fires nothing');
  assert(!s.matchesCombo(altGraph({ ctrlKey: true, altKey: true, key: '€' }), 'ctrl+alt+e'), 'nor typing €');
  // Even where the browser does not implement getModifierState, the shape of
  // the event gives it away: a character that is not a plain letter or digit.
  assert(!s.matchesCombo(ev({ ctrlKey: true, altKey: true, key: '{' }), 'ctrl+alt+b'), 'no AltGraph reported: the character still gives it away');
  assert(!s.matchesCombo(ev({ ctrlKey: true, altKey: true, key: '\\' }), 'ctrl+alt+m'), 'and a backslash');
  assert(!s.matchesCombo(ev({ ctrlKey: true, altKey: true, key: 'æ' }), 'ctrl+alt+a'), 'and a letter outside a-z');
}

// ── What must NOT be mistaken for AltGr ──
{
  assert(s.matchesCombo(ev({ ctrlKey: true, altKey: true, key: 'd' }), 'ctrl+alt+d'), 'a genuine ctrl+alt+letter is not AltGr');
  assert(s.matchesCombo(ev({ ctrlKey: true, altKey: true, key: '5' }), 'ctrl+alt+5'), 'nor ctrl+alt+digit');
}

// ── The ordinary refusals ──
{
  assert(!s.matchesCombo(ev({ key: 'n' }), 'ctrl+alt+n'), 'a bare letter is not the combo');
  assert(!s.matchesCombo(ev({ ctrlKey: true, key: 'n' }), 'ctrl+alt+n'), 'a missing modifier does not match');
  assert(!s.matchesCombo(ev({ ctrlKey: true, altKey: true, shiftKey: true, key: 'n' }), 'ctrl+alt+n'), 'nor an extra one');
  assert(!s.matchesCombo(ev({ ctrlKey: true, key: 'k' }), ''), 'an empty combo matches nothing');
}

// ── An event that does not implement getModifierState must not throw ──
{
  const bare = { ctrlKey: true, altKey: true, shiftKey: false, metaKey: false, key: 'n' };
  let threw = false;
  try {
    s.matchesCombo(bare, 'ctrl+alt+n');
  } catch {
    threw = true;
  }
  assert(!threw, 'a synthetic event without getModifierState is handled, not thrown at');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
