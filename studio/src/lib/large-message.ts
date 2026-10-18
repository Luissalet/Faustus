/** Presentation sections only: the complete message stays in memory and on the wire. */
import { MENTION_RE } from './mentions';
export const LARGE_MESSAGE_THRESHOLD = 16_384;
export const MESSAGE_SECTION_SIZE = 8_192;

export type MessageSection = { start: number; end: number };

function boundary(text: string, end: number): number {
  if (end >= text.length) return text.length;
  const previous = text.charCodeAt(end - 1), next = text.charCodeAt(end);
  if ((previous >= 0xd800 && previous <= 0xdbff && next >= 0xdc00 && next <= 0xdfff)
    || (previous === 13 && next === 10)) return end - 1;
  return end;
}

/** Scan bounded pieces; a message without whitespace must also remain linear. */
export function messageSections(text: string, size = MESSAGE_SECTION_SIZE): MessageSection[] {
  if (!Number.isInteger(size) || size < 4) throw new RangeError('Section size must be at least four');
  const result: MessageSection[] = [];
  for (let start = 0; start < text.length;) {
    let end = Math.min(start + size, text.length);
    if (end < text.length) {
      const tail = text.slice(start, end);
      const space = Math.max(tail.lastIndexOf('\n'), tail.lastIndexOf(' '), tail.lastIndexOf('\t'));
      if (space >= 0) end = start + space + 1;
      // Keep the existing bounded @path / @"path with spaces" chip together.
      const contextStart = Math.max(start, end - 304);
      const context = text.slice(contextStart, Math.min(text.length, end + 304));
      for (const match of context.matchAll(new RegExp(MENTION_RE.source, 'g'))) {
        const first = contextStart + match.index;
        if (first > start && first < end && first + match[0].length > end) end = first;
      }
      end = boundary(text, end);
    }
    result.push({ start, end });
    start = end;
  }
  return result.length ? result : [{ start: 0, end: 0 }];
}

export function sectionAt(sections: MessageSection[], offset: number): number {
  let low = 0, high = sections.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (offset < sections[middle].end) high = middle;
    else low = middle + 1;
  }
  return low;
}

/** Literal full-message search, including matches crossing presentation sections. */
export function findInMessage(text: string, query: string, after = 0): number {
  if (!query) return -1;
  const found = text.indexOf(query, after);
  return found >= 0 || after === 0 ? found : text.indexOf(query);
}

/** A bounded result context also exposes a match that crosses a page boundary. */
export function messageContext(text: string, offset: number): string {
  const start = boundary(text, Math.max(0, offset - 128));
  return text.slice(start, boundary(text, Math.min(text.length, start + MESSAGE_SECTION_SIZE)));
}

export type MessageEdit = {
  text: string;
  sections: MessageSection[];
  index: number;
  draft: string;
};

export function startMessageEdit(text: string, index = 0): MessageEdit {
  const sections = messageSections(text);
  index = Math.max(0, Math.min(index, sections.length - 1));
  const { start, end } = sections[index];
  return { text, sections, index, draft: text.slice(start, end) };
}

export function completeMessageEdit(edit: MessageEdit): string {
  const { start, end } = edit.sections[edit.index];
  return edit.text.slice(0, start) + edit.draft + edit.text.slice(end);
}

export function textareaOffset(text: string, offset: number): number {
  let raw = 0;
  for (let i = 0; i < offset; i++, raw++) {
    if (text[raw] === '\r' && text[raw + 1] === '\n') raw++;
  }
  return raw;
}

/** Textareas normalize line endings. Keep unchanged CR/CRLF bytes outside the edit. */
export function textareaChange(original: string, next: string): string {
  const normalized = original.replace(/\r\n?/g, '\n');
  let prefix = 0, suffix = 0;
  while (prefix < normalized.length && prefix < next.length && normalized[prefix] === next[prefix]) prefix++;
  while (suffix < normalized.length - prefix && suffix < next.length - prefix
    && normalized[normalized.length - 1 - suffix] === next[next.length - 1 - suffix]) suffix++;
  return original.slice(0, textareaOffset(original, prefix)) + next.slice(prefix, next.length - suffix)
    + original.slice(textareaOffset(original, normalized.length - suffix));
}

/** Keep even a large paste out of the mounted textarea; retain all inserted text. */
export function updateMessageEdit(edit: MessageEdit, draft: string, caret = draft.length): { edit: MessageEdit; caret?: number } {
  if (draft.length <= MESSAGE_SECTION_SIZE) return { edit: { ...edit, draft } };
  const changed = { ...edit, draft };
  const text = completeMessageEdit(changed);
  const offset = edit.sections[edit.index].start + caret;
  const sections = messageSections(text);
  const index = sectionAt(sections, Math.max(0, offset - 1));
  const section = sections[index];
  return { edit: { text, sections, index, draft: text.slice(section.start, section.end) },
    caret: Math.min(offset - section.start, section.end - section.start) };
}
