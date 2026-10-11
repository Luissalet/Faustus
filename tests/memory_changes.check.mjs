// #242: the Changes tab of Memory (journal of memory changes) - its words,
// grouping per turn and wiring. Driven by tests/test_memory_changes_ui.py, or by hand:
//   node tests/memory_changes.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
async function bundle(entry, name) {
  const file = join(mkdtempSync(join(tmpdir(), 'fs-mchg-')), name);
  await build({ entryPoints: [join(root, entry)], bundle: true, format: 'esm', platform: 'node', outfile: file, logLevel: 'silent' });
  return import(pathToFileURL(file).href);
}
const read = (p) => readFileSync(join(root, p), 'utf8').replace(/\r\n/g, '\n');
// One bundle, so the model, the adapter and the language switch share the same i18n state.
const entry = join(mkdtempSync(join(tmpdir(), 'fs-mchg-')), 'entry.ts');
await import('node:fs').then(({ writeFileSync }) => writeFileSync(entry, `
export * from ${JSON.stringify(join(root, 'studio/src/screens/memory/changes-model.ts').replace(/\\/g, '/'))};
export { MemoryUndoError, changeFrom } from ${JSON.stringify(join(root, 'studio/src/adapters/memoryJournal.ts').replace(/\\/g, '/'))};
export { setLang, t } from ${JSON.stringify(join(root, 'studio/src/i18n/index.ts').replace(/\\/g, '/'))};
`));
const outFile = join(mkdtempSync(join(tmpdir(), 'fs-mchg-')), 'all.mjs');
await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', outfile: outFile, logLevel: 'silent' });
const m = await import(pathToFileURL(outFile).href);

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };
const eq = (x, y, msg) => assert(JSON.stringify(x) === JSON.stringify(y), `${msg} (got ${JSON.stringify(x)})`);

const row = (id, turnId, extra = {}) => m.changeFrom({ id, ts: 1791644650 + Number(id.replace(/\D/g, '') || 0), store: 'facts', op: 'create', item_id: id, label: id, turn_id: turnId, ...extra });

// -- grouping per turn --
{
  const rows = [row('a1', 'S:1'), row('a2', 'S:1'), row('b1', ''), row('c1', 'S:2'), row('a3', 'S:1')];
  const blocks = m.groupByTurn(rows);
  eq(blocks.map((b) => [b.turnId, b.rows.map((r) => r.id)]),
    [['S:1', ['a1', 'a2', 'a3']], ['', ['b1']], ['S:2', ['c1']]],
    'rows of one turn share a block (placed at the turn\'s newest change), no-turn rows stay alone');
  eq(m.groupByTurn([]), [], 'nothing, no blocks');
}

// -- Spanish wording --
m.setLang('es', { persist: false });
eq(['create', 'edit', 'delete'].map((op) => m.t(m.OP_LABEL[op])), ['Añadido', 'Cambiado', 'Borrado'], 'operation labels agree in the masculine');
eq(m.t('Rolled back'), 'Deshecho', 'an undone change is «Deshecho»');
eq(['ai_agent', 'memory_extractor', 'instinct_extractor', 'user', 'human_explicit', 'auto'].map(m.sourceLabel),
  ['Agente', 'Extractor de memoria', 'Extractor de instintos', 'Tú', 'Tú', 'Automático'], 'sources read as people say them');
eq(m.sourceLabel('some_new-source'), 'some new source', 'an unknown source shows as written, without underscores or hyphens');
eq(['manual', 'promoted', 'session-observation', 'import'].map(m.sourceLabel), ['A mano', 'Promovido por uso', 'Observado en una sesión', 'Importado de un fichero'], 'instinct sources read as people say them');
eq(m.sourceLabel(''), '', 'no source, no chip');
const stamp = 1791644650;
const fmt = (loc) => new Date(stamp * 1000).toLocaleString(loc, { dateStyle: 'short', timeStyle: 'short' });
eq(m.when(stamp), fmt('es-ES'), 'dates use the interface language (es-ES)');
assert(fmt('es-ES') !== fmt('en-GB'), 'es and en dates differ, so the check means something');
eq(m.when(NaN), '', 'a broken timestamp shows nothing');

const later = { id: 'abcdef123456', op: 'edit', label: 'tres' };
const undo = (code, blocking, message = 'server words') => m.undoMessage(new m.MemoryUndoError(message, 409, code, blocking));
eq(undo('later_change', later), 'Deshaz antes el cambio posterior (cambiado): «tres».', 'a refusal names the later change to undo first');
eq(undo('later_change', { ...later, label: '' }), 'Deshaz antes el cambio posterior (cambiado): «abcdef12».', 'with no label it shows the id start');
for (const [code, text] of [
  ['changed_elsewhere', 'El elemento cambió fuera del registro después de este cambio, así que no se puede deshacer.'],
  ['conflict', 'El elemento cambió después de este cambio, así que no se puede deshacer.'],
  ['duplicate', 'El mismo texto se volvió a añadir con otro id, así que no hay nada que recuperar.'],
  ['store_wiped', 'Este almacén se vació después del cambio, así que no se puede deshacer.'],
  ['purged', 'El recuerdo se olvidó del todo y su texto se borró, así que no se puede deshacer.'],
  ['truncated', 'Solo se guardó el principio de este texto tan largo, así que no se puede deshacer.'],
  ['already_undone', 'Este cambio ya se deshizo.'],
  ['unknown', 'Este cambio ya no existe.'],
  ['trash_gone', 'La nota borrada ya no está en la papelera.'],
]) eq(undo(code), text, `refusal «${code}» is in Spanish`);
eq(undo('something_new'), 'server words', 'an unknown code falls back to the server message');
eq(m.undoMessage(new Error('boom')), 'boom', 'a plain error keeps its message');
m.setLang('en', { persist: false });
eq(undo('later_change', later), 'Undo the later change first: changed “tres”.', 'English wording');

// -- wiring --
const tab = read('studio/src/screens/memory/Changes.tsx');
const model = read('studio/src/screens/memory/changes-model.ts');
for (const s of ['memory-changes-clear', 'memory-changes-clear-confirm', 'memory-change-group', 'groupByTurn(', 'clearMemoryChanges', "params.get('turn')", 'listMemoryChanges({ sessionId, turnId, store }'])
  assert(tab.includes(s), `Changes.tsx has ${s}`);
assert(read('studio/src/screens/studio/Transcript.tsx').includes('turn.memoryTurnId ? { turn: turn.memoryTurnId }'), 'the transcript link filters by the turn id');
assert(read('studio/src/screens/studio/model.ts').includes('meta.turn_id'), 'the turn model reads metadata.turn_id');
assert(read('studio/src/adapters/memoryJournal.ts').includes("method: 'DELETE'") && read('studio/src/adapters/memoryJournal.ts').includes('turn_id'), 'the adapter clears the history and filters by turn');
const tsv = read('docs/ui/i18n/es.tsv');
const unq = (x) => x.replace(/\\'/g, "'");
const keys = new Set();
for (const src of [tab, model]) {
  for (const x of src.matchAll(/\bt\('((?:[^'\\]|\\.)*)'/g)) keys.add(unq(x[1]));
  for (const x of src.matchAll(/\btn\([^,]+,\s*'((?:[^'\\]|\\.)*)',\s*'((?:[^'\\]|\\.)*)'/g)) { keys.add(unq(x[1])); keys.add(unq(x[2])); }
}
for (const label of Object.values(m.OP_LABEL).concat(Object.values(m.STORE_LABEL), Object.values(m.SOURCE_LABEL))) keys.add(label);
for (const x of model.matchAll(/t\('([^']*“[^']*)'/g)) keys.add(x[1]);
assert(keys.size > 30, `Changes strings found (${keys.size})`);
const rows = new Set(tsv.split('\n').map((l) => l.split('\t')[0]));
for (const k of keys) assert(rows.has(k), `es.tsv has a row for "${k}"`);

console.log(failed ? `${failed} FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
