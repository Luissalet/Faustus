// Ctrl+K scope commands: `/chats contrato` searches only chats, `/` or `/pro`
// lists the scopes (with Spanish words too: `/proyectos`, `/ajustes`); hit
// titles mark the query's words; projects and settings are sources; a
// settings hit opens Settings > Agent filtered by `?q=`. The pure helpers are
// run for real (the adapter file is transpiled on the fly), the wiring is
// asserted on the source.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import ts from 'typescript';

const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8');
const src = read('../src/adapters/unifiedSearch.ts').replace(/^import .*$/m, 'const getJson = () => null;');
const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText;
const mod = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));

assert.deepEqual(mod.parseScopeCommand('/chats contrato'), { kind: 'run', scope: 'chats', rest: 'contrato' });
assert.deepEqual(mod.parseScopeCommand('/proyectos gulliver'), { kind: 'run', scope: 'projects', rest: 'gulliver' });
assert.deepEqual(mod.parseScopeCommand('/ajustes pasos'), { kind: 'run', scope: 'settings', rest: 'pasos' });
assert.equal(mod.parseScopeCommand('/'). kind, 'list');
assert.ok(mod.parseScopeCommand('/').scopes.length === mod.SEARCH_SOURCES.length, 'every scope listed once');
assert.deepEqual(mod.parseScopeCommand('/pro'), { kind: 'list', scopes: ['projects'] });
assert.equal(mod.parseScopeCommand('/nope algo'), null, 'an unknown word is an ordinary query');
assert.equal(mod.parseScopeCommand('contrato'), null);
assert.deepEqual(mod.matchRanges('Mapas de Gulliver', 'gulliver mapas'), [[0, 5], [9, 17]]);
assert.deepEqual(mod.matchRanges('aaa', 'a aa'), [[0, 3]]);
assert.ok(mod.SEARCH_SOURCES.includes('projects') && mod.SEARCH_SOURCES.includes('settings'));

const pal = read('../src/shell/CommandPalette.tsx');
assert.match(pal, /parseScopeCommand\(query\)/, 'the palette parses scope commands');
assert.match(pal, /data-testid="palette-scopes"/, 'the scope list');
assert.match(pal, /const text = `\/\$\{scope\} `;\s*setQuery\(text\)/, 'picking a scope fills `/scope `');
assert.match(pal, /setSelectionRange\(text\.length, text\.length\)/, '...with the caret after it (cmdk keeps Home/End for the list)');
assert.match(pal, /<Highlighted text=\{hit\.title\}/, 'titles are highlighted');
const settings = read('../src/screens/Settings.tsx');
assert.match(settings, /linkParams\.get\('q'\)/, '?q= pre-fills the agent options filter');
console.log('palette-scopes: ALL OK');
