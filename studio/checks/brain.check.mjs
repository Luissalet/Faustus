// The markdown-vault screen's pure logic (docs/ui build contract, "Lot D"):
// wikilink parsing/rewriting and the quick switcher's fuzzy match
// (studio/src/lib/wikilinks.ts), and the adapter's shape guards — every
// reader has to survive a field being absent or the wrong type, since this
// lot was written and tested before the `/api/brain` routes existed to
// answer it (studio/src/adapters/brain.ts).
//
// Bundled with esbuild on the fly; run by tests/test_brain_studio_js.py, or
// by hand:
//   node studio/checks/brain.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-brain-'));

async function load(rel, name) {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', rel)], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const wiki = await load('lib/wikilinks.ts', 'wikilinks.mjs');
const brain = await load('adapters/brain.ts', 'brain.mjs');

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};
const deepEqual = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/* ── wikilinks: parsing ─────────────────────────────────────────────── */
{
  const links = wiki.parseWikilinks('See [[Ada Lovelace]] and [[Bruno|the other one]], also ![[Bluehaven#Overview]].');
  assert(links.length === 3, 'parses three wikilink occurrences');
  assert(links[0].target === 'Ada Lovelace' && links[0].label === 'Ada Lovelace' && !links[0].isEmbed, 'plain [[target]]: label falls back to target');
  assert(links[1].target === 'Bruno' && links[1].label === 'the other one', '[[target|label]] splits on the pipe');
  assert(links[2].isEmbed && links[2].target === 'Bluehaven' && links[2].heading === 'Overview', '![[target#heading]] is an embed with a heading');
}
{
  const targets = wiki.linkTargets('[[A]] and [[B]] and [[A]] again');
  assert(deepEqual(targets, ['A', 'B']), 'linkTargets is de-duplicated, first-seen order');
}
{
  assert(wiki.foldTitle('Café Bluehaven') === wiki.foldTitle('cafe bluehaven'), 'foldTitle is accent/case-insensitive');
}

/* ── wikilinks: resolving against a title index ────────────────────── */
{
  const index = wiki.buildTitleIndex([{ path: 'Entities/person/Ada.md', title: 'Ada Lovelace' }]);
  assert(wiki.resolveTitle(index, 'ada lovelace').path === 'Entities/person/Ada.md', 'resolveTitle is fold-insensitive');
  assert(wiki.resolveTitle(index, 'Somebody Else') === null, 'resolveTitle misses cleanly on an unknown title');
}

/* ── wikilinks: the href scheme the shared markdown parser leaves alone ── */
{
  const resolvedHref = wiki.wikiHref('Entities/person/Ada.md', true);
  const info = wiki.parseWikiHref(resolvedHref);
  assert(info.kind === 'note' && info.path === 'Entities/person/Ada.md', 'wikiHref/parseWikiHref round-trip (resolved)');
  const unresolvedHref = wiki.wikiHref('Some New Note', false);
  const infoNew = wiki.parseWikiHref(unresolvedHref);
  assert(infoNew.kind === 'note-new' && infoNew.path === 'Some New Note', 'wikiHref/parseWikiHref round-trip (unresolved)');
  assert(wiki.parseWikiHref('https://example.org') === null, 'parseWikiHref rejects an ordinary URL');
  // The one contract this module has to keep with lib/markdown.ts: a hash
  // fragment that is not one of ITS OWN special cases comes back unchanged
  // from safeHref. Ported inline rather than importing markdown.ts, so this
  // check still fails loudly if that contract ever changes shape.
  const looksLikeAScheme = /^[a-z][a-z0-9+.-]*:/i.test(resolvedHref);
  assert(!looksLikeAScheme, 'a wikiHref never looks like a URI scheme to safeHref');
}
{
  const index = wiki.buildTitleIndex([{ path: 'Notes/Bruno.md', title: 'Bruno' }]);
  const rewritten = wiki.rewriteWikilinks('Talk to [[Bruno]] about [[Cordera Labs]].', index);
  assert(rewritten.includes('[Bruno](#brain-note='), 'a resolved link becomes a real markdown link');
  assert(rewritten.includes('[Cordera Labs](#brain-note-new='), 'an unresolved link is still a link, tagged as new');
  const embedded = wiki.rewriteWikilinks('![[Bruno]]', index);
  assert(embedded.startsWith('![Bruno](#brain-embed='), 'an embed becomes a markdown image, not a link');
}

/* ── wikilinks: `[[` autocomplete ──────────────────────────────────── */
{
  const active = wiki.activeWikiAutocomplete('See [[Bru', 9);
  assert(active && active.query === 'Bru' && active.start === 4, 'an open [[query is detected at the caret');
  assert(wiki.activeWikiAutocomplete('See [[Bruno]] here', 18) === null, 'a finished [[…]] does not re-trigger past its close');
  assert(wiki.activeWikiAutocomplete('no brackets here', 5) === null, 'no [[ at all: no autocomplete');
  const applied = wiki.applyWikiAutocomplete('See [[Bru', active, 'Bruno');
  assert(applied.text === 'See [[Bruno]]' && applied.caret === applied.text.length, 'applyWikiAutocomplete splices the title in and closes the brackets');
}

/* ── wikilinks: fuzzy search (the quick switcher) ──────────────────── */
{
  const items = ['Ada Lovelace', 'Bruno', 'Cordera Labs', 'Bluehaven'];
  assert(wiki.fuzzyScore('xyz', 'Ada Lovelace') < 0, 'a subsequence that does not occur scores negative (excluded)');
  assert(wiki.fuzzyScore('ada', 'Ada Lovelace') > wiki.fuzzyScore('a', 'Cordera Labs'), 'a fuller, prefix match beats a scattered one-letter match');
  const ranked = wiki.fuzzySearch('blueh', items, (x) => x);
  assert(ranked[0] === 'Bluehaven', 'fuzzySearch ranks the best match first');
  assert(deepEqual(wiki.fuzzySearch('', items, (x) => x, 2), items.slice(0, 2)), 'an empty query returns the list, capped at the limit');
}

/* ── adapters/brain: every reader survives a missing or malformed field ── */
{
  const status = brain.statusFrom({});
  assert(status.enabled === true && status.notes === 0 && status.lastSync === null, 'statusFrom defaults cleanly on an empty object');
  assert(brain.statusFrom(null).vaultDir === '', 'statusFrom survives null altogether');
}
{
  const tree = brain.noteTreeFrom({ folders: 'not-an-array', notes: [{ path: 'Notes/x.md' }, 'garbage', null] });
  assert(Array.isArray(tree.folders) && tree.folders.length === 0, 'noteTreeFrom drops a malformed folders field to []');
  assert(tree.notes.length === 1, 'noteTreeFrom coerces a bare {path} row and drops the pathless garbage beside it');
  assert(tree.notes[0].title === 'x', 'a note with no title falls back to its filename, extension stripped');
}
{
  const note = brain.readNoteFrom({ path: 'Notes/Idea.md', links: [{ target: 'Missing' }], backlinks: 'nope' });
  assert(note.title === 'Idea', 'readNoteFrom titles a note from its path when the server omits one');
  assert(note.links[0].resolved === false && note.links[0].label === 'Missing', 'a link with no resolved/label flag defaults safely');
  assert(deepEqual(note.backlinks, []), 'a non-array backlinks field becomes []');
  assert(note.editable === true, 'editable defaults true when the field is absent');
}
{
  const graph = brain.brainGraphFrom({ nodes: [{ id: 'Notes/A.md', label: 'A' }, { id: 'Notes/B.md' }], edges: [{ from: 'Notes/A.md', to: 'Notes/B.md' }, { from: 'Notes/A.md', to: 'ghost' }] });
  assert(graph.nodes.length === 2 && graph.nodes[1].label === 'Notes/B.md', 'a node with no label falls back to its id');
  assert(graph.edges.length === 1, 'an edge pointing at an unknown node is dropped, same rule as lib/graph.ts');
}
{
  const profile = brain.entityProfileFrom({ entity: { id: 'e1', name: 'Ada' }, facts: [{ text: 'uses Faustus' }] });
  assert(profile.entity.name === 'Ada' && profile.path === null, 'entityProfileFrom defaults a missing path to null');
  assert(profile.facts[0].validNow === true, 'a fact with no valid_now flag defaults to currently valid');
}
{
  const report = brain.syncReportFrom({ exported: 3, errors: 'not-an-array' });
  assert(report.exported === 3 && deepEqual(report.errors, []), 'syncReportFrom coerces a malformed errors field to []');
}

/* ── adapters/brain: composing a note's file text back from its parts ── */
{
  const note = { path: 'Notes/Idea.md', source: '', generated: '', frontmatter: { tags: ['idea'] } };
  const text = brain.composeNoteContent(note, 'Hello there.');
  assert(text.startsWith('---\ntags: [idea]\n---\n\n'), 'a free note gets a frontmatter block when it has one');
  assert(!text.includes('faustus:generated'), 'a free note with no generated zone gets no marker line');
}
{
  const note = { path: 'Memories/fact/x (a1b2c3d4).md', source: 'mem:a1b2c3d4', generated: '## Validity\nvigente', frontmatter: { kind: 'memory', pinned: true } };
  const text = brain.composeNoteContent(note, 'The user prefers dark mode.');
  assert(text.includes('pinned: true'), 'a boolean frontmatter value is written unquoted');
  assert(text.includes('%% faustus:generated'), 'a generated note keeps the marker line');
  assert(text.trim().endsWith('vigente'), 'the generated zone is carried over unchanged');
}
{
  const yaml = brain.frontmatterToYaml({ tags: ['a', 'b, c'], title: 'Plain', empty: '', missing: undefined });
  assert(yaml.includes('tags: [a, "b, c"]'), 'a list value with a comma inside an item is quoted, the rest are not');
  assert(!yaml.includes('empty') && !yaml.includes('missing'), 'an empty or undefined field is left out of the block entirely');
}

{
  const report = brain.syncReportFrom({ notes: ['a moved to b'], retired: 2 });
  assert(deepEqual(report.notes, ['a moved to b']) && report.retired === 2, 'syncReportFrom reads the phase notes as the list the server sends');
  assert(deepEqual(brain.syncReportFrom({ notes: 'legacy' }).notes, ['legacy']), 'a legacy single-string notes field still reads as one note');
}
{
  const yaml = brain.frontmatterToYaml({ meta: { a: 1 }, refs: [{ u: 'x' }], time: '10:30', ver: '1_000', note: 'yes', when: '2026-09-23T09:17:03Z', day: '2025-01-01', colon: 'a: b' });
  assert(!yaml.includes('[object Object]'), 'a nested mapping is never serialised as [object Object]');
  assert(yaml.includes('meta: {"a":1}') && yaml.includes('refs: [{"u":"x"}]'), 'nested values are written as flow-style JSON, which YAML reads back');
  for (const line of ['time: "10:30"', 'ver: "1_000"', 'note: "yes"', 'when: "2026-09-23T09:17:03Z"', 'day: "2025-01-01"', 'colon: "a: b"']) {
    assert(yaml.includes(line), `a string that could read as another type is quoted (${line})`);
  }
}
{
  const content = '---\n# my own comment\nid: abc\nsource: mem:abc\nkind: memory\ntype: fact\ntime: 10:30\nmeta:\n  nested: 1\naliases:\n- Ada L.\nvalid_until: \'2030-01-01T00:00:00Z\'\n---\n\nText\n\n%% faustus:generated — edits below this line are replaced on the next sync %%\n\n## Validity\n';
  const note = { path: 'Memories/Facts/x (abc).md', source: 'mem:abc', generated: '## Validity', content, frontmatter: { id: 'abc', source: 'mem:abc', kind: 'memory', type: 'fact', time: 630, meta: { nested: 1 }, aliases: ['Ada L.'], valid_until: '2030-01-01T00:00:00Z' } };
  const text = brain.composeNoteContent(note, 'Text', { type: 'preference', valid_until: null });
  assert(text.includes('# my own comment') && text.includes('time: 10:30') && text.includes('meta:\n  nested: 1') && text.includes('aliases:\n- Ada L.'), 'an unpatched key keeps its exact original YAML text');
  assert(text.includes('type: preference') && !text.includes('type: fact'), 'a patched key is rewritten in place');
  assert(text.includes('valid_until: null') && !text.includes('2030-01-01'), 'clearing a field writes an explicit null the server can see');
  assert(brain.composeNoteContent(note, 'Text').startsWith(content.split('\n---\n')[0] + '\n---\n'), 'with no patch the frontmatter block is sent back byte-for-byte');
}
{
  const content = '---\nPrimera seccion, sin dos puntos\n---\nSegunda seccion.\n';
  const note = { path: 'Notes/D.md', source: '', generated: '', content, frontmatter: {} };
  assert(brain.composeNoteContent(note, content.trimEnd()) === content, 'a leading --- block that is not frontmatter stays body text');
}

console.log(failed === 0 ? 'ok brain' : `FAIL: ${failed} assertion(s) failed`);
process.exit(failed === 0 ? 0 : 1);
