// The transcript's Markdown parser (lib/markdown.ts), the mention splitter
// (lib/mentions.ts), the executed-tool-fence stripper (lib/fences.ts) and the
// emoji shortcode table (lib/emoji.ts) —
// pure functions, no React, no browser. Bundled with
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
const fences = await load('fences.ts', 'fences.mjs');
const emoji = await load('emoji.ts', 'emoji.mjs');

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

// ── Executed tool fences ──
{
  const re = fences.fenceRegex(['read_file', 'web_search', 'bash', 'python']);
  const strip = (text) => fences.stripExecutedFences(text, re);

  const call = 'Voy a mirarlo.\n\n```read_file\n{"path": "src/app.py"}\n```\n\nYa está.';
  assert(!strip(call).includes('read_file'), 'an executed tool call goes');
  assert(strip(call).includes('Voy a mirarlo.') && strip(call).includes('Ya está.'), 'and the words around it stay');

  const inline = '```web_search {"query": "faustus"}\n```';
  assert(strip(inline) === '', 'the arguments can be on the fence line');

  const shell = '```bash\nls -la\n```';
  assert(strip(shell) === shell, 'bash is a language, not a tool');
  const py = '```python\nprint(1)\n```';
  assert(strip(py) === py, 'and so is python');

  const unknown = '```make_coffee\n{"sugar": 2}\n```';
  assert(strip(unknown) === unknown, 'a tag that is not a tool is left alone');

  const notJson = '```read_file {title="setup"}\n```';
  assert(strip(notJson) === notJson, 'arguments on the fence line that are not JSON are markdown metadata, not a call');

  const bare = '```read_file\n```';
  assert(strip(bare) === '', 'the tag alone is enough: only a tool call is written that way');

  const prefix = '```read_file_list\n{"a":1}\n```';
  assert(strip(prefix) === prefix, 'a longer tag that merely starts with one is not that tool');

  assert(fences.fenceRegex([]) === null, 'no tags means no regex, never a regex that matches everything');
  assert(fences.fenceRegex(['bash', 'python']) === null, 'a list of only carve-outs is no list at all');
  assert(fences.stripExecutedFences('hola', null) === 'hola', 'with no regex the text is untouched');

  const twice = call + '\n' + call;
  assert(!fences.stripExecutedFences(twice, re).includes('read_file'), 'the regex is reusable: lastIndex does not leak between calls');
}

// ── Emoji shortcodes (lib/emoji.ts) ──────────────────────────────────────────
// Ported from the previous interface (issue #345): models write `:blush:` and
// mean the character. The whole difficulty is knowing when a colon run is NOT
// a shortcode.
{
  const r = emoji.replaceShortcodes;

  assert(r('visit today? :blush:') === 'visit today? \u{1f60a}', 'the shortcode the issue reported converts');
  assert(r('hobbies? **:microphone:**') === 'hobbies? **\u{1f3a4}**', 'markup around a shortcode does not block it');
  assert(r(':fire:') === '\u{1f525}', 'a bare shortcode is the whole string');
  assert(r(':tada:') === '\u{1f389}', 'tada');
  assert(r(':thinking:') === '\u{1f914}', 'thinking');
  assert(r(':+1:') === '\u{1f44d}' && r(':thumbsup:') === '\u{1f44d}', 'aliases land on the same glyph');
  assert(r('nice :fire: work :100:') === 'nice \u{1f525} work \u{1f4af}', 'several in one line');

  assert(r(':definitely_not_an_emoji:') === ':definitely_not_an_emoji:', 'an unknown name is left verbatim');
  assert(r(':emoji:') === ':emoji:', 'the placeholder is not a shortcode');
  assert(r('meet at 10:30:45 today') === 'meet at 10:30:45 today', 'a time is not a shortcode');
  assert(r('ratio 16:9 vs 4:3') === 'ratio 16:9 vs 4:3', 'a ratio is not a shortcode');
  assert(r('plain text') === 'plain text', 'no colons, nothing to do');

  // A KNOWN name inside a longer token is literal text. `1:100:2` is the trap.
  assert(r('1:100:2') === '1:100:2', 'a known name inside a number run stays a number run');
  assert(r('scale 3:100:7 ok') === 'scale 3:100:7 ok', 'and inside a scale');
  assert(r('host:fire:port') === 'host:fire:port', 'an authority-looking string is left alone');
  assert(r('status:fire:') === 'status:fire:', 'glued to a word on the left');
  assert(r(':fire:done') === ':fire:done', 'glued to a word on the right');
  assert(r('we hit :100: today') === 'we hit \u{1f4af} today', 'delimited, it converts');
  assert(r('see :fire:!') === 'see \u{1f525}!', 'punctuation counts as a boundary');
  assert(r(':fire::tada:') === '\u{1f525}\u{1f389}', 'back to back');

  assert(emoji.hasShortcode('a :fire: b') === true, 'the cheap pre-test says yes when there is one');
  assert(emoji.hasShortcode('nothing here') === false, 'and no when there is not');

  // Code is quoted: a snippet about shortcodes must survive being rendered.
  const prose = emoji.replaceShortcodesInProse;
  assert(prose('say :fire:') === 'say \u{1f525}', 'prose converts');
  assert(prose('use `:fire:` here') === 'use `:fire:` here', 'an inline code span is left alone');
  assert(prose('```yaml\nicon: :fire:\n```') === '```yaml\nicon: :fire:\n```', 'a fenced block is left alone');
  assert(prose('a :tada:\n```\n:fire:\n```\nb :fire:') === 'a \u{1f389}\n```\n:fire:\n```\nb \u{1f525}', 'prose either side of a fence still converts');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
