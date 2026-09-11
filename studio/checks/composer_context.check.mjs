// W3-A (CONTRATO_W3.md, ref CONTRATO_CMP_W2.md) — Studio: selection ->
// composer context chip, the `strategy` event, `DocSuggestion.anchor` and
// its client-side auto-disambiguation, "Guardar como receta".
//
// Two halves, same split `strategy.check.mjs`/`doc_session.check.mjs` use:
// pure wire/reducer logic (`decode()`/`apply()`/`restoreFromMetadata()`,
// real modules bundled with esbuild and exercised directly) and JSX wiring
// inside Composer.tsx/SidePanel.tsx/Transcript.tsx (source inspection —
// this is one large component's plumbing, not a bundled import's worth of
// pure logic; same reasoning strategy.check.mjs gives for its own second
// half).
//
// Run by tests/test_w3a_composer_js.py, or by hand:
//   node studio/checks/composer_context.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
async function bundle(entry, outName) {
  const out = join(mkdtempSync(join(tmpdir(), 'fs-w3a-')), outName);
  await build({ entryPoints: [join(root, entry)], bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const { decode } = await bundle('studio/src/adapters/chat.ts', 'chat.mjs');
const { apply, blankTurn, restoreFromMetadata } = await bundle('studio/src/screens/studio/model.ts', 'model.mjs');

// ── `strategy` SSE event: decode() ──
{
  const ev = decode({
    type: 'strategy',
    data: {
      profile: 'fast', recipe_id: 'r1', method: 'plan_then_execute',
      steps: ['step one', 'step two'], budget: { tokens: 100, time_s: 10, calls: 2 },
      reasons: ['task text matched the pattern'],
    },
  }, null);
  assert.equal(ev.type, 'strategy');
  assert.equal(ev.method, 'plan_then_execute');
  assert.equal(ev.profile, 'fast');
  assert.equal(ev.recipeId, 'r1');
  assert.deepEqual(ev.steps, ['step one', 'step two']);
  assert.deepEqual(ev.reasons, ['task text matched the pattern']);
  assert.deepEqual(ev.budget, { tokens: 100, time_s: 10, calls: 2 });

  // No recipe_id on the wire -> null, never "undefined" or an empty string
  // read as a real id.
  const evNoRecipe = decode({ type: 'strategy', data: { profile: 'balanced', method: 'direct_edit' } }, null);
  assert.equal(evNoRecipe.recipeId, null);
}

// ── `strategy` -> Turn.strategy via apply() ──
{
  const ev = decode({ type: 'strategy', data: { profile: 'deep_review', recipe_id: null, method: 'research', steps: ['a'], reasons: ['b'], budget: {} } }, null);
  let turn = blankTurn('assistant');
  turn = apply(turn, ev);
  assert.equal(turn.strategy.method, 'research');
  assert.equal(turn.strategy.profile, 'deep_review');
  assert.equal(turn.strategy.recipeId, null);
  assert.deepEqual(turn.strategy.steps, ['a']);
}

// ── `metadata.strategy` restores on history reload, same shape ──
{
  let turn = blankTurn('assistant');
  turn = restoreFromMetadata(turn, {
    strategy: { profile: 'fast', recipe_id: 'r9', method: 'direct_edit', steps: ['x'], reasons: ['y'], budget: { tokens: 1 } },
  });
  assert.equal(turn.strategy.method, 'direct_edit');
  assert.equal(turn.strategy.recipeId, 'r9');
  assert.deepEqual(turn.strategy.budget, { tokens: 1 });

  // A message with metadata.strategy but nothing else must NOT be skipped
  // by restoreFromMetadata's early-return guard.
  let bare = blankTurn('assistant');
  bare = restoreFromMetadata(bare, { strategy: { profile: 'balanced', method: 'research' } });
  assert.equal(bare.strategy.method, 'research');

  // Absent entirely (older server / no strategy computed): stays undefined,
  // never a crash and never a fabricated default.
  let none = blankTurn('assistant');
  none = restoreFromMetadata(none, { harness: { stop_reason: 'complete' } });
  assert.equal(none.strategy, undefined);
}

// ── `doc_suggestions`: `anchor` decodes, and is absent when the server
// never computed one (older server) ──
{
  const ev = decode({
    type: 'doc_suggestions', doc_id: 'd1',
    suggestions: [
      { id: 's1', find: 'old text', replace: 'new text', reason: 'why', anchor: { quote: 'old text', before: 'lead-in ', after: ' trail-out' } },
      { id: 's2', find: 'other', replace: 'thing', reason: 'why2' }, // no anchor at all
    ],
  }, null);
  assert.equal(ev.type, 'doc_suggestions');
  assert.equal(ev.suggestions.length, 2);
  assert.deepEqual(ev.suggestions[0].anchor, { quote: 'old text', before: 'lead-in ', after: ' trail-out' });
  assert.equal(ev.suggestions[1].anchor, undefined);

  // An anchor with no quote at all is not a usable anchor.
  const evBad = decode({
    type: 'doc_suggestions', doc_id: 'd1',
    suggestions: [{ id: 's3', find: 'x', replace: 'y', reason: 'z', anchor: { before: 'a', after: 'b' } }],
  }, null);
  assert.equal(evBad.suggestions[0].anchor, undefined);
}

// ── SidePanel.tsx: applySuggestion reads the anchor and auto-resolves a
// unique match before ever falling back to the find-only occurrence picker
// (source inspection — this function lives inside a large stateful
// component, not something a bundled import can exercise standalone) ──
{
  const p = 'studio/src/screens/studio/SidePanel.tsx';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes('function suggestionAnchor'), 'SidePanel.tsx must read the anchor off a PendingSuggestion');
  assert.ok(src.includes('function locateAnchor'), 'SidePanel.tsx must mirror locate_quote client-side');
  assert.ok(/const applySuggestion = \(sg: PendingSuggestion\) => \{[\s\S]{0,400}suggestionAnchor\(sg\)/.test(src),
    'applySuggestion must consult the anchor before falling back to find-only occurrence lookup');
  assert.ok(/locateAnchor\(text, anchor\)/.test(src), 'applySuggestion must call locateAnchor with the current text');
  // Never applies just because SOME anchor exists — only when it resolves
  // to exactly one span (`at` truthy after locateAnchor, which itself
  // returns null on zero or many).
  assert.ok(/if \(at\) \{ void applyAt\(sg, at\); return; \}/.test(src),
    'a resolved anchor must apply immediately, without opening the occurrence picker');
}

// ── Composer.tsx: listens for COMPOSER_CONTEXT_EVENT, shows a removable
// chip, and the quoted text never lands in `draft` ──
{
  const p = 'studio/src/screens/studio/Composer.tsx';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes("from '../../lib/docSession'"), 'Composer.tsx must import from lib/docSession');
  assert.ok(src.includes('COMPOSER_CONTEXT_EVENT'), 'Composer.tsx must reference the docSession event name');
  assert.ok(/addEventListener\(COMPOSER_CONTEXT_EVENT/.test(src), 'Composer.tsx must listen for the event');
  assert.ok(src.includes('removeDocContext'), 'the chip must be removable');
  assert.ok(src.includes('data-testid="composer-doc-context"'), 'the chip list must carry a stable testid');
  assert.ok(src.includes('data-testid="composer-doc-context-chip"'), 'each chip must carry a stable testid');
  // The listener stores the event's structured detail (doc/ranges/items) —
  // never feeds `event.detail`'s quoted text into setDraft/draft.
  assert.ok(!/setDraft\([^)]*detail/.test(src), 'the quoted context must never be written into the draft as though the user typed it');
  // Forwarded to the wire via Knobs, mirroring contextOverrides exactly.
  assert.ok(src.includes('docContext?: DocContextRef[]'), 'Knobs must carry docContext for sendTurn to forward');
  assert.ok(src.includes("setKnobs((k) => ({") && /docContext:\s*docContext\.length/.test(src),
    'docContext state must be kept in sync onto knobs.docContext');
  // Cleared once the turn that carried it is actually sent.
  assert.ok(/trySend[\s\S]{0,200}setDocContext\(\[\]\)/.test(src), 'a sent turn must clear its attached document context');
}

// ── adapters/chat.ts: DocContextRef + sendTurn appends doc_context ──
{
  const p = 'studio/src/adapters/chat.ts';
  const src = read(p);
  assert.ok(src.includes('export interface DocContextRef'), 'chat.ts must export DocContextRef');
  assert.ok(src.includes("fd.append('doc_context'"), 'sendTurn must append doc_context onto the form when present');
  assert.ok(src.includes('export interface DocSuggestionAnchor'), 'chat.ts must export DocSuggestionAnchor');
  assert.ok(/anchor\?: DocSuggestionAnchor/.test(src), 'DocSuggestion must declare an optional anchor field');
}

// ── Transcript.tsx: the collapsible "Estrategia: ..." line and "Guardar
// como receta" on a finished turn's TURN SUMMARY ──
{
  const p = 'studio/src/screens/studio/Transcript.tsx';
  const src = read(p);
  assert.ok(src.includes('function StrategyLine'), 'Transcript.tsx must define StrategyLine');
  assert.ok(src.includes('<StrategyLine'), 'StrategyLine must be rendered');
  assert.ok(src.includes('data-testid="turn-strategy"'), 'the strategy line must carry a stable testid, and be collapsible (<details>)');
  assert.ok(/<details className="fs-studio__thinking" data-testid="turn-strategy">/.test(src), 'the strategy line must be a <details> (plegable)');
  assert.ok(src.includes("from '../../adapters/strategy'") && src.includes('createRecipeFromRun'),
    'Transcript.tsx must call createRecipeFromRun, not fetch() /api/recipes/from-run directly');
  assert.ok(!/fetch\(['"`]\/api\/recipes\/from-run/.test(src), 'must go through adapters/strategy.ts');
  assert.ok(src.includes('testId="turn-save-recipe"'), '"Guardar como receta" must carry a stable testid');
  assert.ok(/onNotice\(t\('Saved as a draft recipe: \{id\}'/.test(src), 'a successful save must notify with the draft recipe id');
}

// ── i18n: every new string this lot introduces has a Spanish row ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  const mustHave = [
    'Document context for this message', 'Remove context: {name}', 'Save as recipe',
    'Saved as a draft recipe: {id}', 'Active recipe: {id}', 'Strategy: {method} · {profile}{reason}',
    'fast', 'balanced', 'deep review',
  ];
  for (const key of mustHave) assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
}

console.log('ok composer_context (W3-A)');
