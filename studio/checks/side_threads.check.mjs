// Lote B (CONTRATO_EXCURSOS.md) — Studio: excursos (side threads).
//
// `studio/src/adapters/sideThreads.ts`'s pure helpers (`layerSummaryLine`,
// `formatTokenCount`, `sideThreadName`, `formatAnchor`, `indentSessions`)
// are exercised bundled through esbuild, same pattern
// `studio/checks/l89-git-merge.check.mjs` uses for `adapters/git.ts` — real
// TypeScript, not a Python re-implementation of its logic. The rest is
// static source inspection (`studio/checks/alternatives.check.mjs`'s own
// pattern): the wiring the contract calls out by name — `Transcript.tsx`
// carries `explore-selection`, `Studio.tsx` imports `SideThreadsPanel`, and
// the adapter's one `method: 'DELETE'` is scoped under `/references/` and
// nowhere else.
//
// Run by tests/test_side_threads_js.py, or by hand:
//   node studio/checks/side_threads.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-side-threads-'));

const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

async function load(rel, name) {
  const out = join(dir, name);
  await build({
    entryPoints: [join(root, 'studio', 'src', rel)],
    bundle: true,
    format: 'esm',
    platform: 'node',
    outfile: out,
    logLevel: 'silent',
  });
  return import(pathToFileURL(out).href);
}

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

const mod = await load(join('adapters', 'sideThreads.ts'), 'side-threads.mjs');

// ── layerSummaryLine(): the contract's own worked example ──
// "Enviará ~1.2k tok · 14 mensajes (3 heredados, 1 referencia)" — in the
// check's default English (i18n's `current` defaults to 'en' with no
// `window`/`localStorage`) that is "Will send ~1.2k tok · 14 messages
// (3 inherited, 1 reference)".
{
  const preview = {
    total_tokens: 1234,
    layers: [
      { layer: 'references', messages: 1, tokens: 100, items: [{ session_id: 'x', name: 'X', depth: 'quote', stale: false }] },
      { layer: 'inherited', messages: 3, tokens: 900, from: { session_id: 'p', name: 'Parent', anchor_index: 2, anchor_state: 'ok' } },
      { layer: 'own', messages: 10, tokens: 234 },
    ],
  };
  const line = mod.layerSummaryLine(preview);
  assert(line.includes('~1.2k'), `layerSummaryLine formats total_tokens as ~1.2k: "${line}"`);
  assert(line.includes('14 messages'), `layerSummaryLine sums every layer's messages: "${line}"`);
  assert(line.includes('3 inherited'), `layerSummaryLine names the inherited count: "${line}"`);
  assert(line.includes('1 reference') && !line.includes('1 references'), `layerSummaryLine uses the singular for one reference: "${line}"`);
}

// ── layerSummaryLine(): nothing inherited or referenced -> no parenthetical ──
{
  const preview = {
    total_tokens: 50,
    layers: [
      { layer: 'references', messages: 0, tokens: 0, items: [] },
      { layer: 'inherited', messages: 0, tokens: 0, from: null },
      { layer: 'own', messages: 2, tokens: 50 },
    ],
  };
  const line = mod.layerSummaryLine(preview);
  assert(!/\(\d+ (inherited|reference)/.test(line), `layerSummaryLine omits the inherited/reference parenthetical when there is nothing to report: "${line}"`);
  assert(line.includes('not counted'), `layerSummaryLine says what it does NOT count (system prompt, tools): "${line}"`);
  assert(line.includes('2 messages'), `layerSummaryLine still reports the own-layer count: "${line}"`);
}

// ── formatTokenCount() ──
{
  assert(mod.formatTokenCount(340) === '340', 'formatTokenCount: a plain number under 1000');
  assert(mod.formatTokenCount(1234) === '1.2k', 'formatTokenCount: one decimal in the low thousands');
  assert(mod.formatTokenCount(12000) === '12k', 'formatTokenCount: a whole number at 10k and up');
  assert(mod.formatTokenCount(-5) === '0', 'formatTokenCount: never negative');
}

// ── sideThreadName(): mirrors src/side_threads.py::create_side_thread ──
{
  assert(mod.sideThreadName('a passage here', undefined, 'Parent') === '↳ a passage here', 'sideThreadName prefers the passage');
  assert(mod.sideThreadName(undefined, 'a question', 'Parent') === '↳ a question', 'sideThreadName falls back to the question');
  assert(mod.sideThreadName(undefined, undefined, 'Parent name') === '↳ Parent name', 'sideThreadName falls back to the parent name');
  assert(mod.sideThreadName('', '', 'Parent') === '↳ Parent', 'sideThreadName treats a blank passage/question as absent');
  const long = 'x'.repeat(80);
  assert(mod.sideThreadName(long, undefined, 'Parent').length === 50, 'sideThreadName truncates the base to 48 chars');
}

// ── formatAnchor() ──
{
  assert(mod.formatAnchor(0, 'ok') === 'from turn 1', 'formatAnchor: anchor_index is 0-based, shown 1-based');
  assert(mod.formatAnchor(4, 'ok') === 'from turn 5', 'formatAnchor: anchor_index 4 reads as turn 5');
  assert(mod.formatAnchor(null, 'missing') === 'the source turn no longer exists', 'formatAnchor: missing anchor_state wins regardless of anchor_index');
  assert(mod.formatAnchor(4, 'missing') === 'the source turn no longer exists', 'formatAnchor: missing anchor_state ignores a present anchor_index');
  assert(mod.formatAnchor(null, 'ok') === 'from an earlier turn', 'formatAnchor: no anchor_index but state ok still says something');
}

// ── indentSessions(): SessionsPane.tsx's own ordering/depth helper ──
{
  const list = [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }];
  // b is a's child, d is b's child (grandchild of a), c's "parent" is not
  // in this list at all (a different sort/filter — must stay at depth 0).
  const parents = { b: 'a', d: 'b', c: 'zzz-not-in-list' };
  const out = mod.indentSessions(list, parents);
  assert(out.map((o) => o.item.id).join(',') === 'a,b,d,c', `indentSessions draws a child right after its parent: ${out.map((o) => o.item.id).join(',')}`);
  assert(out.find((o) => o.item.id === 'a').depth === 0, 'indentSessions: a root session is depth 0');
  assert(out.find((o) => o.item.id === 'b').depth === 1, 'indentSessions: a child is depth 1');
  assert(out.find((o) => o.item.id === 'd').depth === 2, 'indentSessions: a grandchild is depth 2');
  assert(out.find((o) => o.item.id === 'c').depth === 0, "indentSessions: a parent absent from the list never indents its child");
  assert(out.length === list.length, 'indentSessions never drops or duplicates a session');

  // A self-referencing / cyclic parents map must never hang or crash.
  const cyclic = { a: 'a' };
  const selfOut = mod.indentSessions([{ id: 'a' }], cyclic);
  assert(selfOut.length === 1 && selfOut[0].depth === 0, 'indentSessions: a session naming itself as its own parent is ignored, not a cycle');
}

// ── addMaterial/updateMaterial/removeMaterial/getStaleTurns: exported, and
//    keep the server's own field names (F2/F1, CONTRATO_CABLES2) ──
{
  const src = read('studio/src/adapters/sideThreads.ts');
  for (const symbol of ['addMaterial', 'updateMaterial', 'removeMaterial', 'getStaleTurns']) {
    assert(src.includes(`export function ${symbol}`), `sideThreads.ts must export ${symbol}()`);
  }
  for (const field of ['document_id', 'note_text', 'quotes', 'ranges', 'stale_turns', 'last_index']) {
    assert(src.includes(field), `sideThreads.ts must keep the server's own field name ${JSON.stringify(field)}`);
  }
  assert(src.includes('/materials'), 'sideThreads.ts must call the /materials routes');
  assert(src.includes('/stale-turns'), 'sideThreads.ts must call GET .../stale-turns');
}

// ── static wiring: the contract's own named checks ──

// The adapter's `method: 'DELETE'` calls — one for references, one for
// materials (CONTRATO_CABLES2 F2) — and each is scoped exactly where it
// says: never a bare session, never a branch wire.
{
  const src = read('studio/src/adapters/sideThreads.ts');
  const matches = [...src.matchAll(/\{\s*method:\s*'DELETE'/g)];
  assert(matches.length === 2, `sideThreads.ts must use method: 'DELETE' exactly twice — references and materials (found ${matches.length})`);
  const scopedUnder = (needle, path) => {
    const idx = src.indexOf(needle);
    assert(idx !== -1, `sideThreads.ts must define ${needle}`);
    if (idx === -1) return;
    const fnStart = src.lastIndexOf('export function', idx);
    const fnEnd = src.indexOf('\n}', idx);
    const fnBody = src.slice(fnStart, fnEnd === -1 ? undefined : fnEnd);
    assert(fnBody.includes(path), `${needle} must be scoped under a ${path} path`);
    assert(fnBody.includes("method: 'DELETE'"), `${needle} must actually issue a DELETE`);
  };
  scopedUnder('export function removeReference', '/references/');
  scopedUnder('export function removeMaterial', '/materials/');
}

// ── adapters/condense.ts (F3, new): exported, and the range's own routes ──
{
  const src = read('studio/src/adapters/condense.ts');
  for (const symbol of ['previewCondense', 'condense', 'expandCondensed']) {
    assert(src.includes(`export function ${symbol}`), `condense.ts must export ${symbol}()`);
  }
  assert(src.includes('/condense/preview'), 'condense.ts must call GET .../condense/preview');
  assert(src.includes('/condense/'), 'condense.ts must call .../condense and .../condense/{index}/expand');
  assert(src.includes('expand'), 'condense.ts must call the .../expand route');
}

// ── adapters/chat.ts::loadHistory (F3): keeps a condensed system row, uses
//    the server's own index, and HistoryMessage.role admits 'system' ──
{
  const src = read('studio/src/adapters/chat.ts');
  assert(/role:\s*'user'\s*\|\s*'assistant'\s*\|\s*'system'/.test(src), "chat.ts's HistoryMessage.role must admit 'system'");
  assert(src.includes('meta.condensed'), 'loadHistory must keep a system row only when metadata.condensed is set');
}

// Transcript.tsx: the floating "Explorar aparte" button and the turn's own
// "Explorar desde aquí" action both exist and both flow through onExplore.
{
  const src = read('studio/src/screens/studio/Transcript.tsx');
  assert(src.includes("data-testid=\"explore-selection\""), 'Transcript.tsx must render the explore-selection button');
  assert(src.includes('onExplore'), 'Transcript.tsx must accept/thread an onExplore prop');
  assert(src.includes('testId="turn-explore"'), 'Transcript.tsx must render the per-turn "Explorar desde aquí" action');
  assert(src.includes('historyIndex'), 'Transcript.tsx must pass historyIndex to onExplore, not a raw array index alone');
}

// Transcript.tsx (CONTRATO_CABLES2): F1's stale-wire banner + regenerate,
// F3's "Condense up to here" action and the condensed-row card + Expand.
{
  const src = read('studio/src/screens/studio/Transcript.tsx');
  assert(src.includes('data-testid="turn-stale-wire"'), 'Transcript.tsx must render the F1 stale-wire banner');
  assert(src.includes('staleWires'), 'Transcript.tsx must read Turn.staleWires');
  assert(src.includes('testId="turn-condense"'), 'Transcript.tsx must render the per-turn "Condense up to here" action');
  assert(src.includes('onCondense'), 'Transcript.tsx must accept/thread an onCondense prop');
  assert(src.includes('data-testid="turn-condensed"'), 'Transcript.tsx must render the condensed-row card');
  assert(src.includes('testId="turn-expand"'), 'Transcript.tsx must render the condensed row\'s Expand action');
  assert(src.includes("turn.role === 'system'"), "Transcript.tsx must branch a condensed row off turn.role === 'system'");
}

// Composer.tsx (F2): the document-context chip gains a "Fijar" pin button
// that calls addMaterial, hidden when there is no session yet.
{
  const src = read('studio/src/screens/studio/Composer.tsx');
  assert(src.includes('data-testid="doc-context-pin"'), 'Composer.tsx must render the doc-context-pin button');
  assert(src.includes('addMaterial'), 'Composer.tsx must call addMaterial() from its pin button');
  assert(/sessionId\s*&&[\s\S]{0,400}doc-context-pin/.test(src), 'Composer.tsx must gate the pin button on a real sessionId (no session yet = nothing to wire into)');
}

// SideThreadsPanel.tsx (F1/F2): the Materials section, its add-note form,
// and the wire-replay button for a wire whose stale_turns.count > 0.
{
  const src = read('studio/src/screens/studio/SideThreadsPanel.tsx');
  assert(src.includes('testId="material-add-note"'), 'SideThreadsPanel.tsx must render the "Add a note" control');
  assert(src.includes('testId="wire-replay"'), 'SideThreadsPanel.tsx must render the wire-replay button for stale turns');
  assert(src.includes('onRegenerateTurn'), 'SideThreadsPanel.tsx must accept an onRegenerateTurn prop');
  assert(src.includes('addMaterial') && src.includes('updateMaterial') && src.includes('removeMaterial'), 'SideThreadsPanel.tsx must use the materials adapter functions');
  assert(src.includes("t('Context wires')"), 'SideThreadsPanel.tsx must use the renamed "Context wires" title');
}

// Studio.tsx: mounts SideThreadsPanel and ExploreDialog, and wires onExplore
// into Transcript — never re-implementing either screen inline.
{
  const src = read('studio/src/screens/Studio.tsx');
  assert(src.includes("from './studio/SideThreadsPanel'"), 'Studio.tsx must import SideThreadsPanel');
  assert(src.includes('<SideThreadsPanel'), 'Studio.tsx must render SideThreadsPanel');
  assert(src.includes("from './studio/ExploreDialog'"), 'Studio.tsx must import ExploreDialog');
  assert(src.includes('<ExploreDialog'), 'Studio.tsx must render ExploreDialog');
  assert(src.includes('onExplore={onExplore}'), 'Studio.tsx must wire onExplore into <Transcript>');
  assert(src.includes('data-testid="studio-open-side-threads"') || src.includes("testId=\"studio-open-side-threads\""), 'Studio.tsx must expose a way to open the side threads panel');
}

// Studio.tsx (CONTRATO_CABLES2): the popover's label moves to "Context
// wires"; CondenseDialog is mounted and wired; turnsFromHistory uses the
// SERVER's own m.index instead of re-numbering its filtered array.
{
  const src = read('studio/src/screens/Studio.tsx');
  assert(src.includes("t('Context wires')"), 'Studio.tsx must rename the side-threads popover trigger to "Context wires"');
  assert(!src.includes("t('Side threads')"), 'Studio.tsx must not still show the old "Side threads" popover label');
  assert(src.includes("from './studio/CondenseDialog'"), 'Studio.tsx must import CondenseDialog');
  assert(src.includes('<CondenseDialog'), 'Studio.tsx must render CondenseDialog');
  assert(src.includes('onCondense={') && src.includes('onCondense'), 'Studio.tsx must wire onCondense into <Transcript>');
  assert(src.includes('onExpandCondensed'), 'Studio.tsx must wire onExpandCondensed into <Transcript>');
  assert(src.includes('onRegenerateTurn={onRegenerateTurn}'), 'Studio.tsx must wire onRegenerateTurn into <SideThreadsPanel>');
  assert(src.includes('refreshStaleWires'), 'Studio.tsx must fetch/apply the F1 stale-turns map');
  // The bug this fixes: turnsFromHistory must read `m.index` (the server's
  // own history position loadHistory already assigned), not re-number the
  // array it filters turns from — see adapters/chat.ts::loadHistory's own
  // doc comment on why a condensed row ahead of a later turn breaks that.
  assert(/historyIndex:\s*m\.index/.test(src), 'Studio.tsx::turnsFromHistory must use m.index as historyIndex, not the filtered array\'s own position');
}

// SessionsPane.tsx: fetches the parents map once and indents with it.
{
  const src = read('studio/src/screens/studio/SessionsPane.tsx');
  assert(src.includes('getParentsMap'), 'SessionsPane.tsx must call getParentsMap()');
  assert(src.includes('indentSessions'), 'SessionsPane.tsx must indent rows with indentSessions()');
}

// The new files this lote owns actually exist.
for (const p of [
  'studio/src/adapters/sideThreads.ts',
  'studio/src/adapters/condense.ts',
  'studio/src/screens/studio/SideThreadsPanel.tsx',
  'studio/src/screens/studio/ExploreDialog.tsx',
  'studio/src/screens/studio/CondenseDialog.tsx',
]) {
  assert(existsSync(path(p)), `missing ${p}`);
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nSide threads (Lote B, CONTRATO_EXCURSOS.md + CONTRATO_CABLES2 Lote B): all checks passed');
