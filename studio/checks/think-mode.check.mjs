// Lot T: the composer's reasoning-mode chip (Auto / Fast / Think / Deep).
// adapters/composer.ts (parseThinkMode, thinkModeChipText, readThinkMode/
// writeThinkMode), adapters/chat.ts (decode of the `think_mode` SSE event)
// and the wiring in Composer.tsx / Studio.tsx / commands.ts.
// Bundled with esbuild on the fly; run by tests/test_think_mode_js.py, or:
//   node studio/checks/think-mode.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-think-mode-'));
const bundle = async (entry, name) => {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', ...entry)], bundle: true, format: 'esm',
    platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
};

let failed = 0;
const assert = (cond, msg) => {
  if (!cond) { failed += 1; console.error('FAIL:', msg); }
  else console.log('ok:', msg);
};

// A minimal localStorage so the per-chat persistence can be exercised.
const store = new Map();
globalThis.window = globalThis.window ?? {};
globalThis.window.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: (k) => { store.delete(k); },
};

const c = await bundle(['adapters', 'composer.ts'], 'composer.mjs');
{
  assert(JSON.stringify(c.THINK_MODES) === JSON.stringify(['auto', 'fast', 'think', 'deep']), 'four modes, Auto first');
  assert(c.parseThinkMode('auto') === 'auto', 'auto parses');
  assert(c.parseThinkMode('Rápido') === 'fast', 'Spanish rápido is fast');
  assert(c.parseThinkMode('pensar') === 'think', 'Spanish pensar is think');
  assert(c.parseThinkMode('a  fondo') === 'deep', 'Spanish a fondo is deep');
  assert(c.parseThinkMode('on') === null && c.parseThinkMode('off') === null,
    'on/off are not modes: /think on|off keeps pinning the switch');
  assert(c.parseThinkMode(undefined) === null && c.parseThinkMode(3) === null, 'junk is no mode');
  assert(c.thinkModeChipText('auto', 'think') === 'Auto · Think', 'after an Auto turn the chip says what Auto chose');
  assert(c.thinkModeChipText('auto', null) === 'Auto', 'before any turn it just says Auto');
  assert(c.thinkModeChipText('deep', 'fast') === 'Deep', 'an explicit pick shows the pick');
  c.writeThinkMode('s1', 'deep');
  assert(c.readThinkMode('s1') === 'deep', 'the pick is kept per chat');
  assert(c.readThinkMode('s2') === null, 'another chat has no pick of its own');
  assert(c.readThinkMode(null) === null, 'no chat, no pick');
  const saved = globalThis.window.localStorage;
  globalThis.window.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  let threw = false;
  try { c.writeThinkMode('s3', 'fast'); c.readThinkMode('s3'); } catch { threw = true; }
  assert(!threw, 'blocked storage never throws');
  globalThis.window.localStorage = saved;
}

const chat = await bundle(['adapters', 'chat.ts'], 'chat.mjs');
{
  const ev = chat.decode({ type: 'think_mode', data: { mode: 'deep', requested: 'auto', source: 'rule', reasons: ['asked_depth'], budget: 16384 } }, null);
  assert(ev && ev.type === 'think_mode' && ev.mode === 'deep' && ev.budget === 16384, 'the think_mode event decodes');
  const bad = chat.decode({ type: 'think_mode', data: { mode: 'weird' } }, null);
  assert(bad && bad.mode === 'fast' && bad.budget === null, 'an unknown mode decodes as fast, no budget');
}

// ── wiring (source-level) ──
{
  const composer = readFileSync(join(root, 'studio', 'src', 'screens', 'studio', 'Composer.tsx'), 'utf8');
  assert(composer.includes('function ThinkModeChip'), 'the chip component exists');
  assert(composer.includes('onSetThinkMode && supportsThinking(modelName)'), 'the chip shows only for a thinking model');
  const studio = readFileSync(join(root, 'studio', 'src', 'screens', 'Studio.tsx'), 'utf8');
  assert(studio.includes('thinkMode,') && studio.includes("think_mode_default"), 'the send carries the mode; the default comes from settings');
  assert(studio.includes("name === 'think' ? parseThinkMode(args)"), '/think auto|fast|think|deep picks the mode');
  const chatSrc = readFileSync(join(root, 'studio', 'src', 'adapters', 'chat.ts'), 'utf8');
  assert(chatSrc.includes("fd.append('think_mode', options.thinkMode)"), 'think_mode is posted as a form field');
  const cmds = readFileSync(join(root, 'studio', 'src', 'screens', 'studio', 'commands.ts'), 'utf8');
  assert(cmds.includes('/think auto|fast|think|deep|on|off'), 'the command usage lists the modes and keeps on/off');
}

console.log(failed ? `${failed} failure(s)` : 'ok think_mode');
process.exit(failed ? 1 : 0);
