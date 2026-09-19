// CMP-GEN: the composer's generation chip -> real sampling controls
// (studio/src/adapters/composer.ts: supportsThinking, genEffectiveValue,
// genFieldSource, genWithOverride, genWithoutOverride) and the think-model
// pattern list mirrored from `src/llm_core.py`'s `_THINKING_MODEL_PATTERNS`.
// Bundled with esbuild on the fly; run by tests/test_cmp_gen_panel_js.py,
// or by hand:
//   node studio/checks/gen-sampling-panel.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-gen-panel-')), 'composer.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'adapters', 'composer.ts')],
  bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent',
});
const c = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (cond, msg) => {
  if (!cond) { failed += 1; console.error('FAIL:', msg); }
  else console.log('ok:', msg);
};

// ── supportsThinking: mirrors src/llm_core.py's _THINKING_MODEL_PATTERNS ──
{
  assert(c.supportsThinking('qwen3:32b') === true, 'qwen3 is thinking-capable');
  assert(c.supportsThinking('deepseek-r1:14b') === true, 'deepseek-r1 is thinking-capable');
  assert(c.supportsThinking('Magistral-Small-2509') === true, 'match is case-insensitive');
  assert(c.supportsThinking('llama3.1:8b') === false, 'a plain llama model has no think switch');
  assert(c.supportsThinking(null) === false, 'no model name never claims support');
  assert(c.supportsThinking(undefined) === false, 'undefined model name never claims support');
  assert(c.supportsThinking('') === false, 'empty model name never claims support');
}

// ── genEffectiveValue: override wins, else the global default ──
{
  const defaults = { temperature: 0.6, top_p: 0.8, top_k: 20 };
  assert(c.genEffectiveValue('temperature', {}, defaults) === 0.6, 'no override -> the global default');
  assert(c.genEffectiveValue('temperature', { temperature: 1.2 }, defaults) === 1.2, 'an explicit override wins');
  assert(c.genEffectiveValue('top_p', { temperature: 1.2 }, defaults) === 0.8, 'other fields keep their own default');
  assert(c.genEffectiveValue('max_tokens', {}, defaults) === undefined, 'max_tokens has no global default (SET-07)');
  assert(c.genEffectiveValue('max_tokens', { max_tokens: 2048 }, defaults) === 2048, 'max_tokens override still applies');
  assert(c.genEffectiveValue('think', {}, defaults) === undefined, 'think has no global default either');
  assert(c.genEffectiveValue('think', { think: true }, defaults) === true, 'think override applies');
}

// ── genFieldSource: which label a control shows ──
{
  assert(c.genFieldSource('temperature', {}) === 'default', 'unset reads as default');
  assert(c.genFieldSource('temperature', { temperature: 0.9 }) === 'override', 'set reads as override');
  assert(c.genFieldSource('think', { think: false }) === 'override', 'an explicit false is still an override, not "unset"');
}

// ── genWithOverride / genWithoutOverride: one field at a time, the rest untouched ──
{
  const gen = { temperature: 0.9, top_p: 0.5 };
  const next = c.genWithOverride(gen, 'top_k', 40);
  assert(next.top_k === 40 && next.temperature === 0.9 && next.top_p === 0.5, 'setting one field keeps the others');
  assert(gen.top_k === undefined, 'the original object is untouched (no mutation)');

  const cleared = c.genWithoutOverride(next, 'top_k');
  assert(cleared.top_k === undefined, 'reset removes just that field');
  assert(cleared.temperature === 0.9 && cleared.top_p === 0.5, 'and leaves every other override alone');
}

// ── describeGen: unchanged, still drives the chip label / X visibility ──
{
  assert(c.describeGen({}) === '', 'no overrides -> no label -> the chip shows "Generation" and no X');
  assert(c.describeGen({ temperature: 0.4 }).includes('0.4'), 'a set override still shows in the summary');
}

// ── Composer.tsx wiring: the chip opens a real panel, not just a static label ──
{
  const src = readFileSync(join(root, 'studio', 'src', 'screens', 'studio', 'Composer.tsx'), 'utf8');
  assert(src.includes('function GenSettingsPopover'), 'the sampling panel component exists');
  assert(src.includes("id=\"gen-temperature\""), 'a real temperature control, not just describeGen() text');
  assert(src.includes("id=\"gen-top-p\""), 'a real top_p control');
  assert(src.includes("id=\"gen-top-k\""), 'a real top_k control');
  assert(src.includes("id=\"gen-max-tokens\""), 'a real max_tokens control');
  assert(src.includes('thinkApplies') && src.includes('supportsThinking(modelName)'),
    'the think switch is gated by the same capability check as the backend, not always shown');
  assert(src.includes('onClearGen') && src.includes('fs-studio__chip-x'),
    'the outer X still clears every override at once');
  assert(src.includes('onSetGen(genWithOverride') && src.includes('onSetGen(genWithoutOverride'),
    'each control writes/resets through the shared GenOverrides setter');
}

console.log(failed ? `${failed} failure(s)` : 'ok gen_sampling_panel');
process.exit(failed ? 1 : 0);
