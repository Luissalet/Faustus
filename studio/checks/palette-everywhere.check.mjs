// Ctrl+K "Everywhere": the palette searches every kind of owned thing through
// GET /api/search/all once the query has two characters, debounced, with a
// filter row by type; results are force-mounted (cmdk must not filter the
// server's matches away) and open their own screen. Static source assertions.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8');
const adapter = read('../src/adapters/unifiedSearch.ts');
assert.match(adapter, /\/api\/search\/all\?/, 'the adapter calls GET /api/search/all');
assert.match(adapter, /'chats', 'brain', 'notes', 'documents', 'gallery', 'skills', 'board'/, 'the seven sources');
const pal = read('../src/shell/CommandPalette.tsx');
assert.match(pal, /q\.length < 2/, 'two characters before searching');
assert.match(pal, /setTimeout\([\s\S]*?250\)/, 'debounced');
assert.match(pal, /data-testid="palette-filters"/, 'the filter row');
assert.match(pal, /forceMount\s+onSelect=\{\(\) => go\(hit\.url\)\}/, 'server hits are force-mounted and navigate');
assert.match(pal, /value=\{query\} onValueChange=\{setQuery\}/, 'the input is controlled');
const board = read('../src/screens/board/BoardPanel.tsx');
assert.match(board, /get\('issue'\)/, '?issue=<id> opens that issue');
console.log('palette-everywhere: ALL OK');
