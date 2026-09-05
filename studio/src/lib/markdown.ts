/**
 * Markdown, for what a model actually writes into a transcript.
 *
 * The old reader (rich.tsx) handled fenced code, bold, links, headings and
 * flat lists, and let everything else fall through as text. That is a safe
 * failure — nothing is hidden — but a comparison table arrives as a wall of
 * pipes and a six-level plan arrives as six identical lines, so the reply is
 * readable without being legible.
 *
 * This is the whole grammar a reply uses: headings with real levels, nested
 * and loose lists, task lists, tables with alignment, blockquotes, rules,
 * footnotes, images, emphasis, strikethrough and autolinks. It is a parser,
 * not a renderer: it returns a tree, rich.tsx turns it into React, and the
 * pure half is what the tests can hold. ~9 KB of source against the 40-90 KB
 * a library would have cost (DECISIONES_UI.md).
 *
 * Unknown syntax still falls through as text. That rule has not changed.
 */

export type Align = 'left' | 'center' | 'right' | null;
export type Level = 1 | 2 | 3 | 4 | 5 | 6;

export type Inline =
  | { kind: 'text'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'strong'; children: Inline[] }
  | { kind: 'em'; children: Inline[] }
  | { kind: 'del'; children: Inline[] }
  | { kind: 'link'; href: string; children: Inline[] }
  | { kind: 'image'; src: string; alt: string }
  | { kind: 'note'; id: string; index: number }
  | { kind: 'break' };

export interface ListItem {
  /** A `- [ ]` item; undefined when the item is an ordinary bullet. */
  task?: boolean;
  done?: boolean;
  blocks: Block[];
}

export type Block =
  | { kind: 'heading'; level: Level; children: Inline[] }
  | { kind: 'para'; children: Inline[] }
  | { kind: 'code'; lang: string; code: string }
  | { kind: 'list'; ordered: boolean; start: number; items: ListItem[] }
  | { kind: 'quote'; blocks: Block[] }
  | { kind: 'table'; head: Inline[][]; align: Align[]; rows: Inline[][][] }
  | { kind: 'rule' };

export interface Footnote {
  id: string;
  index: number;
  blocks: Block[];
}

export interface Parsed {
  blocks: Block[];
  footnotes: Footnote[];
}

interface Ctx {
  order: string[];
  defs: Map<string, string>;
}

/* ── Inline ─────────────────────────────────────────────────────── */

/**
 * One alternation, tried in order at each position, so `**` wins over `*`,
 * `![` over `[`, and a link's URL is consumed before the bare-URL rule can
 * see it. `_` needs a word boundary or snake_case would come out italic.
 */
const INLINE = new RegExp(
  [
    '\\\\[\\\\`*_{}\\[\\]()#+\\-.!>~|]',
    '``[^`]+``',
    '`[^`\\n]+`',
    '\\*\\*(?=\\S)[\\s\\S]*?\\S\\*\\*',
    '__(?=\\S)[\\s\\S]*?\\S__',
    '~~(?=\\S)[\\s\\S]*?\\S~~',
    '!\\[[^\\]\\n]*\\]\\([^\\s)]*\\)',
    '\\[\\^[^\\]\\s]+\\]',
    '\\[[^\\]\\n]*\\]\\([^\\s)]*(?:\\s+"[^"\\n]*")?\\)',
    '<https?://[^>\\s]+>',
    '\\*(?=\\S)[^*\\n]*?\\S\\*',
    '(?<![\\w\\\\])_(?=\\S)[^_\\n]*?\\S_(?![\\w])',
    'https?://[^\\s<>()\\[\\]]+',
  ].join('|'),
  'g',
);

const ESCAPED = new RegExp('\\\\([\\\\`*_{}\\[\\]()#+\\-.!>~|])', 'g');

/** A model can write any URL; only these schemes get to be a live link. */
export function safeHref(href: string): string {
  const value = href.trim().split(/\s+/)[0] ?? '';
  if (/^[a-z][a-z0-9+.-]*:/i.test(value)) return /^(?:https?|mailto):/i.test(value) ? value : '#';
  return value || '#';
}

/**
 * A link to somewhere else on the web, from a source we did not write.
 *
 * Search results, a model's citation list, a page a tool fetched: the URL is
 * data from outside, and `javascript:` in an `href` is a script that runs on
 * click. React escapes the VALUE of an attribute, not its scheme, so the
 * whitelist has to be here. `null` means "do not make this a link at all" —
 * showing the title as plain text is the honest fallback.
 */
export function safeExternal(url: unknown): string | null {
  const value = String(url ?? '').trim();
  if (!value) return null;
  // Control characters are how `java\0script:` gets past a naive check.
  const clean = value.replace(/[\u0000-\u001f\u007f-\u009f]/g, '');
  return /^https?:\/\//i.test(clean) ? clean : null;
}

/**
 * A picture in the model's markdown.
 *
 * A data URL is allowed only for the raster types: `data:image/svg+xml` is
 * markup, not a bitmap, and an SVG loaded through `<img>` still runs its own
 * `onload` in some browsers — and is a script outright the moment anything
 * inlines it.
 */
export function safeSrc(src: string): string {
  const value = src.trim();
  if (/^data:/i.test(value)) return /^data:image\/(?:png|jpe?g|gif|webp);/i.test(value) ? value : '#';
  return safeHref(value);
}

/** Adjacent runs merge, so an escape never splits a word into three nodes. */
function pushText(out: Inline[], raw: string): void {
  const text = raw.replace(ESCAPED, '$1');
  if (!text) return;
  const last = out[out.length - 1];
  if (last && last.kind === 'text') last.text += text;
  else out.push({ kind: 'text', text });
}

function noteIndex(ctx: Ctx, id: string): number {
  const at = ctx.order.indexOf(id);
  if (at >= 0) return at + 1;
  ctx.order.push(id);
  return ctx.order.length;
}

function parseInline(text: string, ctx: Ctx): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    const at = match.index ?? 0;
    if (at < last) continue;
    if (at > last) pushText(out, text.slice(last, at));
    const token = match[0];
    last = at + token.length;
    // A backslash-escaped punctuation mark is literal: it must be taken out
    // of the scan before `*` or `[` gets a chance to open a span with it.
    if (token.length === 2 && token.startsWith('\\')) pushText(out, token[1]);
    else if (token.startsWith('``')) out.push({ kind: 'code', text: token.slice(2, -2).trim() });
    else if (token.startsWith('`')) out.push({ kind: 'code', text: token.slice(1, -1) });
    else if (token.startsWith('**')) out.push({ kind: 'strong', children: parseInline(token.slice(2, -2), ctx) });
    else if (token.startsWith('__')) out.push({ kind: 'strong', children: parseInline(token.slice(2, -2), ctx) });
    else if (token.startsWith('~~')) out.push({ kind: 'del', children: parseInline(token.slice(2, -2), ctx) });
    else if (token.startsWith('![')) {
      const m = /^!\[([^\]]*)\]\(([^\s)]*)\)/.exec(token);
      out.push({ kind: 'image', alt: m?.[1] ?? '', src: safeSrc(m?.[2] ?? '') });
    } else if (token.startsWith('[^')) {
      const id = token.slice(2, -1);
      out.push({ kind: 'note', id, index: noteIndex(ctx, id) });
    } else if (token.startsWith('[')) {
      const m = /^\[([^\]]*)\]\(([^\s)]*)/.exec(token);
      const label = m?.[1] ?? '';
      out.push({ kind: 'link', href: safeHref(m?.[2] ?? ''), children: label ? parseInline(label, ctx) : [{ kind: 'text', text: safeHref(m?.[2] ?? '') }] });
    } else if (token.startsWith('<')) {
      const url = token.slice(1, -1);
      out.push({ kind: 'link', href: safeHref(url), children: [{ kind: 'text', text: url }] });
    } else if (token.startsWith('*') || token.startsWith('_')) {
      out.push({ kind: 'em', children: parseInline(token.slice(1, -1), ctx) });
    } else {
      // A bare URL swallows the sentence's full stop otherwise.
      const url = token.replace(/[.,;:!?)\]]+$/, '');
      out.push({ kind: 'link', href: url, children: [{ kind: 'text', text: url }] });
      last = at + url.length;
    }
  }
  if (last < text.length) pushText(out, text.slice(last));
  return out;
}

/** A paragraph keeps the breaks the model typed: they are usually meant. */
function parseInlineLines(lines: string[], ctx: Ctx): Inline[] {
  const out: Inline[] = [];
  lines.forEach((line, i) => {
    if (i > 0) out.push({ kind: 'break' });
    out.push(...parseInline(line, ctx));
  });
  return out;
}

/* ── Blocks ─────────────────────────────────────────────────────── */

const FENCE = /^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)/;
const RULE = /^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/;
const ATX = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const QUOTE = /^\s{0,3}>/;
const ITEM = /^(\s*)([-*+•]|\d{1,9}[.)])(\s+)(.*)$/;
const SETEXT = /^\s{0,3}=+\s*$/;

function indentOf(line: string): number {
  let n = 0;
  for (const ch of line) {
    if (ch === ' ') n += 1;
    else if (ch === '\t') n += 4;
    else break;
  }
  return n;
}

function dedent(line: string, columns: number): string {
  let i = 0;
  let seen = 0;
  while (i < line.length && seen < columns) {
    if (line[i] === ' ') seen += 1;
    else if (line[i] === '\t') seen += 4;
    else break;
    i += 1;
  }
  return line.slice(i);
}

export function isDelimiterRow(line: string): boolean {
  const s = line.trim();
  if (!s.includes('|') || !s.includes('-')) return false;
  return /^\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?$/.test(s);
}

/** `a | b\|c | d` — a cell can carry an escaped pipe, so no plain split. */
export function splitRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const cells: string[] = [];
  let cur = '';
  for (let i = 0; i < s.length; i += 1) {
    if (s[i] === '\\' && s[i + 1] === '|') {
      cur += '|';
      i += 1;
    } else if (s[i] === '|') {
      cells.push(cur.trim());
      cur = '';
    } else {
      cur += s[i];
    }
  }
  cells.push(cur.trim());
  return cells;
}

function alignFrom(line: string): Align[] {
  return splitRow(line).map((cell) => {
    const left = cell.startsWith(':');
    const right = cell.endsWith(':');
    if (left && right) return 'center';
    if (right) return 'right';
    if (left) return 'left';
    return null;
  });
}

function startsBlock(lines: string[], i: number): boolean {
  const line = lines[i];
  if (FENCE.test(line) || RULE.test(line) || ATX.test(line) || QUOTE.test(line) || ITEM.test(line)) return true;
  return line.includes('|') && i + 1 < lines.length && isDelimiterRow(lines[i + 1]);
}

function parseList(lines: string[], start: number, ctx: Ctx): [Block, number] {
  const first = ITEM.exec(lines[start]);
  if (!first) return [{ kind: 'para', children: parseInline(lines[start], ctx) }, start + 1];
  const base = first[1].length;
  const ordered = /\d/.test(first[2]);
  const startNo = ordered ? Number.parseInt(first[2], 10) : 1;
  const items: ListItem[] = [];
  let i = start;
  while (i < lines.length) {
    const m = ITEM.exec(lines[i]);
    if (!m) break;
    const indent = m[1].length;
    if (indent < base || indent > base + 3) break;
    if (/\d/.test(m[2]) !== ordered) break;
    const column = indent + m[2].length + m[3].length;
    const buf: string[] = [m[4]];
    i += 1;
    while (i < lines.length) {
      const line = lines[i];
      if (line.trim() === '') {
        const next = lines[i + 1];
        if (next !== undefined && next.trim() !== '' && indentOf(next) >= column) {
          buf.push('');
          i += 1;
          continue;
        }
        break;
      }
      if (indentOf(line) >= column) {
        buf.push(dedent(line, column));
        i += 1;
        continue;
      }
      if (ITEM.test(line) || startsBlock(lines, i)) break;
      buf.push(line.trim());
      i += 1;
    }
    const task = /^\[([ xX])\]\s+([\s\S]*)$/.exec(buf[0] ?? '');
    if (task) {
      buf[0] = task[2];
      items.push({ task: true, done: task[1] !== ' ', blocks: parseBlocks(buf, ctx) });
    } else {
      items.push({ blocks: parseBlocks(buf, ctx) });
    }
  }
  if (!items.length) return [{ kind: 'para', children: parseInline(lines[start], ctx) }, start + 1];
  return [{ kind: 'list', ordered, start: startNo, items }, i];
}

function parseBlocks(lines: string[], ctx: Ctx): Block[] {
  const out: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim() === '') {
      i += 1;
      continue;
    }

    const fence = FENCE.exec(line);
    if (fence) {
      const close = fence[1][0] === '`' ? /^\s{0,3}`{3,}\s*$/ : /^\s{0,3}~{3,}\s*$/;
      const buf: string[] = [];
      i += 1;
      while (i < lines.length && !close.test(lines[i])) {
        buf.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      out.push({ kind: 'code', lang: fence[2] ?? '', code: buf.join('\n') });
      continue;
    }

    if (RULE.test(line)) {
      out.push({ kind: 'rule' });
      i += 1;
      continue;
    }

    const atx = ATX.exec(line);
    if (atx) {
      out.push({ kind: 'heading', level: atx[1].length as Level, children: parseInline(atx[2], ctx) });
      i += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && (QUOTE.test(lines[i]) || (lines[i].trim() !== '' && buf.length > 0 && !startsBlock(lines, i)))) {
        buf.push(lines[i].replace(/^\s{0,3}>\s?/, ''));
        i += 1;
      }
      out.push({ kind: 'quote', blocks: parseBlocks(buf, ctx) });
      continue;
    }

    if (line.includes('|') && i + 1 < lines.length && isDelimiterRow(lines[i + 1])) {
      const head = splitRow(line);
      const align = alignFrom(lines[i + 1]);
      if (align.length === head.length) {
        i += 2;
        const rows: Inline[][][] = [];
        while (i < lines.length && lines[i].trim() !== '' && lines[i].includes('|')) {
          const cells = splitRow(lines[i]);
          while (cells.length < head.length) cells.push('');
          rows.push(cells.slice(0, head.length).map((cell) => parseInline(cell, ctx)));
          i += 1;
        }
        out.push({ kind: 'table', head: head.map((cell) => parseInline(cell, ctx)), align, rows });
        continue;
      }
    }

    if (ITEM.test(line)) {
      const [block, next] = parseList(lines, i, ctx);
      out.push(block);
      i = next;
      continue;
    }

    const para: string[] = [];
    while (i < lines.length && lines[i].trim() !== '' && !startsBlock(lines, i)) {
      para.push(lines[i]);
      i += 1;
      if (i < lines.length && SETEXT.test(lines[i])) {
        out.push({ kind: 'heading', level: 1, children: parseInlineLines(para, ctx) });
        para.length = 0;
        i += 1;
        break;
      }
    }
    if (para.length) out.push({ kind: 'para', children: parseInlineLines(para, ctx) });
  }
  return out;
}

/* ── Footnote definitions ───────────────────────────────────────── */

/** `[^n]: text` lines are pulled out first so they never render inline. */
function extractNotes(text: string, defs: Map<string, string>): string {
  const lines = text.split('\n');
  const kept: string[] = [];
  let fenced = false;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (FENCE.test(line)) {
      fenced = !fenced;
      kept.push(line);
      i += 1;
      continue;
    }
    const def = fenced ? null : /^\s{0,3}\[\^([^\]\s]+)\]:\s?([\s\S]*)$/.exec(line);
    if (def) {
      const body = [def[2]];
      i += 1;
      while (i < lines.length && /^(?: {4}|\t)/.test(lines[i])) {
        body.push(dedent(lines[i], 4));
        i += 1;
      }
      defs.set(def[1], body.join('\n'));
      continue;
    }
    kept.push(line);
    i += 1;
  }
  return kept.join('\n');
}

export function parseMarkdown(raw: string): Parsed {
  const defs = new Map<string, string>();
  const body = extractNotes(raw.replace(/\r\n?/g, '\n'), defs);
  const ctx: Ctx = { order: [], defs };
  const blocks = parseBlocks(body.split('\n'), ctx);
  const footnotes: Footnote[] = [];
  const seen = new Set<string>();
  // Referenced notes first, in the order the text calls them.
  for (let i = 0; i < ctx.order.length; i += 1) {
    const id = ctx.order[i];
    if (seen.has(id)) continue;
    seen.add(id);
    const text = defs.get(id);
    footnotes.push({
      id,
      index: i + 1,
      blocks: text === undefined ? [] : parseBlocks(text.split('\n'), ctx),
    });
  }
  // A definition nothing points at still gets shown: nothing is lost.
  let n = footnotes.length;
  for (const [id, text] of defs) {
    if (seen.has(id)) continue;
    seen.add(id);
    n += 1;
    footnotes.push({ id, index: n, blocks: parseBlocks(text.split('\n'), ctx) });
  }
  return { blocks, footnotes };
}

/** The plain text of a tree — for titles, previews and copy. */
export function inlineText(nodes: Inline[]): string {
  return nodes
    .map((node) => {
      switch (node.kind) {
        case 'text':
        case 'code':
          return node.text;
        case 'image':
          return node.alt;
        case 'note':
          return `[${node.index}]`;
        case 'break':
          return ' ';
        default:
          return inlineText(node.children);
      }
    })
    .join('');
}
