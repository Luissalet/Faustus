// The transcript's Markdown parser (lib/markdown.ts) and the mention splitter
// (lib/mentions.ts) — pure functions, no React, no browser. Bundled with
// esbuild on the fly; run by tests/test_studio_markdown_js.py, or by hand:
//   node studio/checks/markdown.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-md-'));

async function load(rel, name) {
  const out = join(dir, name);
  await build({ entryPoints: [join(root, 'studio', 'src', 'lib', rel)], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const md = await load('markdown.ts', 'markdown.mjs');
const mentions = await load('mentions.ts', 'mentions.mjs');

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};
const parse = (text) => md.parseMarkdown(text);
const text = (nodes) => md.inlineText(nodes);

// ── Headings ──
{
  const { blocks } = parse('# Uno\n\n### Tres\n\nTexto suelto.');
  assert(blocks[0].kind === 'heading' && blocks[0].level === 1, 'h1');
  assert(blocks[1].kind === 'heading' && blocks[1].level === 3, 'h3 keeps its level');
  assert(blocks[2].kind === 'para', 'a paragraph after a heading');
  assert(parse('Titulo\n===\n').blocks[0].level === 1, 'setext h1');
  assert(parse('#hashtag no es titulo').blocks[0].kind === 'para', 'a hash with no space is not a heading');
}

// ── Inline ──
{
  const nodes = parse('**a** *b* _c_ ~~d~~ `e` [f](https://x.y) https://bare.url/p.').blocks[0].children;
  const kinds = nodes.map((n) => n.kind).filter((k) => k !== 'text');
  assert(kinds.join(',') === 'strong,em,em,del,code,link,link', `inline kinds: ${kinds.join(',')}`);
  assert(nodes[nodes.length - 2].href === 'https://bare.url/p', 'a bare URL drops the sentence full stop');
  assert(text(parse('snake_case_word aqui').blocks[0].children) === 'snake_case_word aqui', 'snake_case does not go italic');
  assert(parse('a `**no**` b').blocks[0].children.some((n) => n.kind === 'code' && n.text === '**no**'), 'code wins over bold');
  assert(parse('\\*literal\\*').blocks[0].children[0].text === '*literal*', 'backslash escapes');
  assert(parse('[x](javascript:alert(1))').blocks[0].children[0].href === '#', 'a javascript: link is defused');
  assert(parse('[x](/local/path)').blocks[0].children[0].href === '/local/path', 'a relative link survives');
  assert(parse('![alt](https://x.y/a.png)').blocks[0].children[0].kind === 'image', 'image');
}

// ── Lists ──
{
  const list = parse('1. uno\n2. dos\n   - anidado\n   - otro\n3. tres').blocks[0];
  assert(list.kind === 'list' && list.ordered && list.items.length === 3, 'three ordered items');
  const nested = list.items[1].blocks.find((b) => b.kind === 'list');
  assert(nested && !nested.ordered && nested.items.length === 2, 'the nested bullets hang off the second item');
  const tasks = parse('- [ ] pendiente\n- [x] hecho\n- normal').blocks[0];
  assert(tasks.items[0].task && !tasks.items[0].done, 'unchecked task');
  assert(tasks.items[1].task && tasks.items[1].done, 'checked task');
  assert(!tasks.items[2].task, 'a plain bullet is not a task');
  assert(parse('7. siete\n8. ocho').blocks[0].start === 7, 'an ordered list keeps its first number');
}

// ── Tables ──
{
  const table = parse('| A | B | C |\n| :- | -: | :-: |\n| 1 | 2 | 3 |\n| 4 | 5 | 6 |').blocks[0];
  assert(table.kind === 'table', 'a table');
  assert(table.align.join(',') === 'left,right,center', 'alignment row');
  assert(table.rows.length === 2 && text(table.rows[1][2]) === '6', 'two body rows');
  assert(text(parse('| a | b |\n| - | - |\n| x \\| y | z |').blocks[0].rows[0][0]) === 'x | y', 'an escaped pipe stays in the cell');
  assert(parse('solo texto\n---\notra cosa').blocks[1].kind === 'rule', 'a bare --- is a rule, not a table');
  const short = parse('| a | b |\n| - | - |\n| solo |').blocks[0];
  assert(short.rows[0].length === 2, 'a short row is padded, never dropped');
}

// ── Quotes, rules, code ──
{
  const quote = parse('> uno\n> **dos**').blocks[0];
  assert(quote.kind === 'quote' && quote.blocks[0].kind === 'para', 'blockquote');
  assert(parse('***').blocks[0].kind === 'rule', 'a *** rule');
  const code = parse('```python\nx = 1\n```').blocks[0];
  assert(code.kind === 'code' && code.lang === 'python' && code.code === 'x = 1', 'fenced code with a language');
  assert(parse('```\n# no es titulo\n```').blocks[0].code === '# no es titulo', 'a fence hides block syntax');
  assert(parse('```\nsin cerrar').blocks[0].code === 'sin cerrar', 'an unclosed fence still renders');
}

// ── Footnotes ──
{
  const p = parse('Texto[^b] y luego[^a].\n\n[^a]: la de a.\n[^b]: la de b.');
  assert(p.blocks[0].kind === 'para', 'the definitions leave the body');
  assert(p.footnotes.length === 2, 'two footnotes');
  assert(p.footnotes[0].id === 'b' && p.footnotes[0].index === 1, 'numbered by first reference, not by definition');
  assert(text(p.footnotes[0].blocks[0].children) === 'la de b.', 'the note carries its own text');
  const orphan = parse('sin referencias\n\n[^z]: nadie me llama.');
  assert(orphan.footnotes.length === 1, 'a definition nobody points at is still shown');
}

// ── Nothing is ever lost ──
{
  const weird = '<<< raro >>> {no markdown} 50% | suelto';
  assert(text(parse(weird).blocks[0].children) === weird, 'unknown syntax falls through as text');
  assert(parse('').blocks.length === 0, 'empty input');
  assert(parse('a\r\nb').blocks[0].children.some((n) => n.kind === 'break'), 'CRLF and the line break inside a paragraph');
}

// ── Mentions ──
{
  const parts = mentions.splitMentions('mira @src/app.py, y @"con espacios.txt" luego.');
  const found = parts.filter((p) => p.mention).map((p) => p.mention);
  assert(found.join('|') === 'src/app.py|con espacios.txt', `mentions: ${found.join('|')}`);
  assert(parts.some((p) => p.text && p.text.startsWith(',')), 'the trailing comma stays out of the path');
  assert(!mentions.hasMention('un correo a alguien@example.com'), 'an email address is not a mention');
  assert(mentions.mentionPath(undefined, 'a/b.py.') === 'a/b.py', 'trailing punctuation trimmed');
  assert(mentions.splitMentions('sin arrobas').length === 1, 'text with no mention comes back whole');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
