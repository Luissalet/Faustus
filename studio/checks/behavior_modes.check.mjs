// CONTRATO_MODOS Lote B (Studio) — behaviour modes' pure presentation
// helpers, driven through esbuild rather than re-implemented in Python —
// same pattern `studio/checks/bench.check.mjs`/`side_threads.check.mjs` use.
//
// Two halves:
//   1. `studio/src/adapters/behaviorModes.ts`'s pure functions: `modeLabel`
//      in both languages, `modeDescription`, `violationsLabel`, and
//      `resolveModeCommand`'s four outcomes (bare list, an id, a name, off,
//      an unknown one).
//   2. A static grep guard: neither `Composer.tsx` nor `Studio.tsx` ever
//      calls `setSessionMode(` from inside a `useEffect(...)` — a mode must
//      only ever change because someone picked it (the chip's popover,
//      `/mode`, or the first-send persist in `Studio.tsx`'s own `send()`),
//      never on its own as a side effect of mounting or reconnecting.
//
// Run by tests/test_behavior_modes_js.py, or by hand:
//   node studio/checks/behavior_modes.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-modes-')), 'behaviorModes.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'adapters', 'behaviorModes.ts')],
  bundle: true,
  format: 'esm',
  platform: 'node',
  outfile: out,
  logLevel: 'silent',
});
const modes = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

// ── Fixtures: a small catalog shaped exactly like Mode.to_dict() ──────────
const DEFAULT_MODE = {
  id: 'default', builtin: true, version: 1,
  name: { en: 'Default', es: 'Por defecto' },
  description: { en: 'Faustus as it is: no extra stance.', es: 'Faustus tal cual: sin postura añadida.' },
  prompt: '', checks: {},
};
const ADVERSARIAL = {
  id: 'adversarial', builtin: true, version: 1,
  name: { en: 'Adversarial', es: 'Adversarial' },
  description: { en: 'Challenges every claim before agreeing.', es: 'Cuestiona cada afirmación antes de darte la razón.' },
  prompt: 'Never start with agreement.\nAlways tag confidence: [Certain]/[Likely]/[Guessing].\nNever say "Great question".',
  checks: { first_sentence: 'challenge', confidence_tags: true, forbidden_phrases: ['great question'] },
};
const SOCRATIC = {
  id: 'socratic', builtin: true, version: 1,
  name: { en: 'Socratic', es: 'Socrático' },
  description: { en: 'Answers with questions.', es: 'Responde con preguntas.' },
  prompt: 'Prefer a guiding question over a direct answer.',
  checks: { ends_with_question: true },
};
const CATALOG = [DEFAULT_MODE, ADVERSARIAL, SOCRATIC];

// ── modeLabel: both languages, and a graceful fallback for null/undefined ──
assert(modes.modeLabel(ADVERSARIAL, 'en') === 'Adversarial', 'modeLabel: English name');
assert(modes.modeLabel(ADVERSARIAL, 'es') === 'Adversarial', 'modeLabel: Spanish name (same word here)');
assert(modes.modeLabel(SOCRATIC, 'es') === 'Socrático', 'modeLabel: Spanish name differs from English');
assert(modes.modeLabel(null, 'en') === 'Default', 'modeLabel(null): falls back to the literal word Default');
assert(modes.modeLabel({ id: 'x', name: { en: '', es: '' } }, 'en') === 'x', 'modeLabel: empty names fall back to the raw id, never a blank chip');

// ── modeDescription: language, and English fallback for a missing ES row ──
assert(modes.modeDescription(SOCRATIC, 'es') === 'Responde con preguntas.', 'modeDescription: Spanish');
assert(modes.modeDescription({ description: { en: 'only english', es: '' } }, 'es') === 'only english', 'modeDescription: falls back to English when the ES row is empty');

// ── violationsLabel: the server's own detail strings, verbatim ───────────
assert(modes.violationsLabel(null) === '', 'violationsLabel(null) is empty, not "undefined"');
assert(modes.violationsLabel({ checked: ['confidence_tags'], violations: [] }) === '', 'violationsLabel: nothing violated is empty');
assert(
  modes.violationsLabel({
    checked: ['first_sentence', 'forbidden_phrases'],
    violations: [
      { rule: 'first_sentence', detail: 'first sentence opens with agreement' },
      { rule: 'forbidden_phrases', detail: 'used a banned phrase: "absolutely"' },
    ],
  }) === 'first sentence opens with agreement · used a banned phrase: "absolutely"',
  'violationsLabel: joins every detail, in order, unchanged',
);

// ── findModeByIdOrName: id, exact name, partial name, both languages ──────
assert(modes.findModeByIdOrName(CATALOG, 'adversarial', 'en')?.id === 'adversarial', 'findModeByIdOrName: exact id');
assert(modes.findModeByIdOrName(CATALOG, 'Socrático', 'es')?.id === 'socratic', 'findModeByIdOrName: exact Spanish name');
assert(modes.findModeByIdOrName(CATALOG, 'socrat', 'en')?.id === 'socratic', 'findModeByIdOrName: partial name match');
assert(modes.findModeByIdOrName(CATALOG, 'nope', 'en') === undefined, 'findModeByIdOrName: no match is undefined, not a throw');
assert(modes.findModeByIdOrName(CATALOG, '   ', 'en') === undefined, 'findModeByIdOrName: blank input is undefined');

// ── resolveModeCommand: /mode's four outcomes ─────────────────────────────
const bare = modes.resolveModeCommand(CATALOG, 'default', '', 'en');
assert(bare.kind === 'list', 'resolveModeCommand(""): bare lists');
assert(bare.markdown.includes('**Default**'), 'resolveModeCommand(""): the active one is marked');
assert(!bare.markdown.includes('**Adversarial**'), 'resolveModeCommand(""): an inactive one is not marked');

const byId = modes.resolveModeCommand(CATALOG, 'default', 'adversarial', 'en');
assert(byId.kind === 'set' && byId.id === 'adversarial', 'resolveModeCommand("adversarial"): sets by id');

const byName = modes.resolveModeCommand(CATALOG, 'default', 'Socrático', 'es');
assert(byName.kind === 'set' && byName.id === 'socratic', 'resolveModeCommand("Socrático"): sets by Spanish name');

for (const off of ['off', 'Off', 'no', 'ninguno', 'default', 'por defecto', 'por_defecto']) {
  const result = modes.resolveModeCommand(CATALOG, 'adversarial', off, 'en');
  assert(result.kind === 'set' && result.id === 'default', `resolveModeCommand("${off}"): always the literal default mode`);
}

const unknown = modes.resolveModeCommand(CATALOG, 'default', 'nonexistent-mode', 'en');
assert(unknown.kind === 'unknown' && typeof unknown.markdown === 'string' && unknown.markdown.length > 0, 'resolveModeCommand("nonexistent-mode"): unknown, with a pointer back to /mode');

// ── Static guard: `setSessionMode(` never called from inside a useEffect ──
// Same balanced-paren technique tests/test_inf04_bench_js.py's
// `_call_bodies` uses for `startBench(`/`useEffect` in Optimize.tsx.
function callBodies(text, callee) {
  const bodies = [];
  const marker = `${callee}(`;
  let idx = 0;
  for (;;) {
    const pos = text.indexOf(marker, idx);
    if (pos === -1) break;
    const start = pos + marker.length;
    let depth = 1;
    let i = start;
    while (i < text.length && depth > 0) {
      if (text[i] === '(') depth += 1;
      else if (text[i] === ')') depth -= 1;
      i += 1;
    }
    bodies.push(text.slice(start, i));
    idx = i;
  }
  return bodies;
}

for (const relPath of ['screens/studio/Composer.tsx', 'screens/Studio.tsx']) {
  const text = readFileSync(join(root, 'studio', 'src', relPath), 'utf8');
  const effectBodies = callBodies(text, 'useEffect');
  assert(effectBodies.length > 0, `${relPath} has at least one useEffect (otherwise this guard checks nothing)`);
  const offender = effectBodies.find((body) => body.includes('setSessionMode('));
  assert(offender === undefined, `${relPath}: setSessionMode(...) must never be called from inside a useEffect`);
}

// The other half of the same rule, made concrete: Studio.tsx DOES call
// setSessionMode somewhere outside any effect (the picker/`/mode`/first
// send) — otherwise the guard above would trivially pass by the feature
// never being wired up at all.
const studioSrc = readFileSync(join(root, 'studio', 'src', 'screens', 'Studio.tsx'), 'utf8');
assert(studioSrc.includes('setSessionMode('), 'Studio.tsx never calls setSessionMode(...) anywhere — the picker/`/mode` must call it explicitly');

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nCONTRATO_MODOS Lote B: all checks passed');
