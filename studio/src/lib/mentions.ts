/**
 * The `@path` tokens in a sent message.
 *
 * The composer's `@` picker writes them; the server resolves them from the
 * message text (src/file_mentions.py); this splits them back out so the
 * transcript can show which files a turn actually pointed at, and open one
 * without leaving the chat. Purely cosmetic: the turn means the same thing
 * with or without the chips.
 *
 * MENTION_RE mirrors the pattern in src/file_mentions.py, and the same
 * pattern the previous interface used (static/js/mentionChips.js).
 */

export const MENTION_RE = new RegExp(
  '(?<![\\w@/\\\\.-])@(?:"([^"\\n]{1,300})"|([A-Za-z0-9_.][\\w./\\\\-]{0,299}))',
  'g',
);

export interface MentionPart {
  /** A run of ordinary text. */
  text?: string;
  /** The path the token points at, and the token exactly as written. */
  mention?: string;
  token?: string;
}

/** The path a mention token refers to, trailing sentence punctuation removed. */
export function mentionPath(quoted?: string, bare?: string): string {
  let raw = (quoted || bare || '').trim();
  while (raw && '.,;:!?'.includes(raw.slice(-1))) raw = raw.slice(0, -1);
  return raw;
}

/** Split a message into plain runs and mentions, in order. */
export function splitMentions(text: string): MentionPart[] {
  const out: MentionPart[] = [];
  let last = 0;
  const re = new RegExp(MENTION_RE.source, 'g');
  let match = re.exec(text);
  while (match !== null) {
    const path = mentionPath(match[1], match[2]);
    if (path) {
      // The token as written, minus any punctuation the path dropped.
      const written = match[1] || match[2] || '';
      const token = match[0].slice(0, match[0].length - (written.length - path.length));
      if (match.index > last) out.push({ text: text.slice(last, match.index) });
      out.push({ mention: path, token });
      last = match.index + token.length;
      re.lastIndex = last;
    }
    match = re.exec(text);
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out;
}

/** True when the text has at least one mention worth decorating. */
export function hasMention(text: string): boolean {
  return text.includes('@') && splitMentions(text).some((part) => part.mention);
}
