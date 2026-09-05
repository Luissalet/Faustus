/** Toolbar edits on a textarea's text + selection. Pure, testable, undo-friendly. */
export interface Edit {
  text: string;
  start: number;
  end: number;
}

export type MdAction = 'bold' | 'italic' | 'strike' | 'code' | 'h1' | 'h2' | 'h3' | 'quote' | 'ul' | 'ol' | 'task' | 'codeblock' | 'link' | 'hr' | 'table';

function lineBounds(text: string, start: number, end: number): { from: number; to: number } {
  const from = text.lastIndexOf('\n', start - 1) + 1;
  const nl = text.indexOf('\n', end);
  return { from, to: nl < 0 ? text.length : nl };
}

function wrap(text: string, start: number, end: number, mark: string): Edit {
  const sel = text.slice(start, end);
  const before = text.slice(Math.max(0, start - mark.length), start), after = text.slice(end, end + mark.length);
  if (before === mark && after === mark) {
    return { text: text.slice(0, start - mark.length) + sel + text.slice(end + mark.length), start: start - mark.length, end: end - mark.length };
  }
  if (sel.startsWith(mark) && sel.endsWith(mark) && sel.length >= mark.length * 2) {
    const inner = sel.slice(mark.length, sel.length - mark.length);
    return { text: text.slice(0, start) + inner + text.slice(end), start, end: start + inner.length };
  }
  const body = sel || 'text';
  return { text: text.slice(0, start) + mark + body + mark + text.slice(end), start: start + mark.length, end: start + mark.length + body.length };
}

function prefixLines(text: string, start: number, end: number, make: (i: number, line: string) => string, strip: RegExp): Edit {
  const { from, to } = lineBounds(text, start, end);
  const lines = text.slice(from, to).split('\n');
  const allOn = lines.every((l) => strip.test(l));
  const out = lines.map((l, i) => (allOn ? l.replace(strip, '') : make(i, l.replace(strip, ''))));
  const block = out.join('\n');
  return { text: text.slice(0, from) + block + text.slice(to), start: from, end: from + block.length };
}

export function applyMarkdown(action: MdAction, text: string, start: number, end: number): Edit {
  switch (action) {
    case 'bold':
      return wrap(text, start, end, '**');
    case 'italic':
      return wrap(text, start, end, '*');
    case 'strike':
      return wrap(text, start, end, '~~');
    case 'code':
      return wrap(text, start, end, '`');
    case 'h1':
    case 'h2':
    case 'h3': {
      const hashes = '#'.repeat(Number(action[1]));
      const { from, to } = lineBounds(text, start, end);
      const line = text.slice(from, to);
      const bare = line.replace(/^#{1,6}\s+/, '');
      const next = line.startsWith(hashes + ' ') ? bare : `${hashes} ${bare}`;
      return { text: text.slice(0, from) + next + text.slice(to), start: from, end: from + next.length };
    }
    case 'quote':
      return prefixLines(text, start, end, (_, l) => `> ${l}`, /^>\s?/);
    case 'ul':
      return prefixLines(text, start, end, (_, l) => `- ${l}`, /^[-*]\s+/);
    case 'ol':
      return prefixLines(text, start, end, (i, l) => `${i + 1}. ${l}`, /^\d+\.\s+/);
    case 'task':
      return prefixLines(text, start, end, (_, l) => `- [ ] ${l}`, /^[-*]\s+\[[ xX]\]\s+/);
    case 'codeblock': {
      const sel = text.slice(start, end) || 'code';
      const block = `\n\`\`\`\n${sel}\n\`\`\`\n`;
      return { text: text.slice(0, start) + block + text.slice(end), start: start + 5, end: start + 5 + sel.length };
    }
    case 'link': {
      const sel = text.slice(start, end) || 'link';
      const out = `[${sel}](https://)`;
      return { text: text.slice(0, start) + out + text.slice(end), start: start + sel.length + 3, end: start + sel.length + 11 };
    }
    case 'hr': {
      const out = '\n\n---\n\n';
      return { text: text.slice(0, start) + out + text.slice(end), start: start + out.length, end: start + out.length };
    }
    case 'table': {
      const out = '\n| Column | Column |\n| --- | --- |\n| cell | cell |\n';
      return { text: text.slice(0, start) + out + text.slice(end), start: start + 3, end: start + 9 };
    }
  }
}

export const RUNNABLE = new Set(['python', 'py', 'bash', 'sh', 'shell', 'zsh', 'javascript', 'js', 'html']);
export const PREVIEWABLE = new Set(['markdown', 'md', '', 'text', 'csv', 'html']);

/** Minimal CSV parser (quotes, commas, newlines) for the table preview. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], cell = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i++;
        } else quoted = false;
      } else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === ',' || c === '\t' || c === ';') {
      row.push(cell);
      cell = '';
    } else if (c === '\n') {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = '';
    } else if (c !== '\r') cell += c;
  }
  if (cell !== '' || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

/** Pen colour of the signature pad: ink on paper, whatever the theme. */
export const SIGNATURE_INK = '#111111';
