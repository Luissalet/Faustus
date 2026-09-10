import { matchCommands, type Suggestion } from './commands';

/**
 * UX-06: what the `@`/`/` box under the caret should show, from the draft
 * text alone — no network, no React state. Split out of `Composer.tsx` so
 * it can run behind `frameBatcher` (studio/src/lib/frame-batch.ts, the same
 * mechanism `Transcript.tsx` already uses to cap streaming repaints at one
 * per frame): a burst of keystrokes inside one animation frame calls this
 * exactly once instead of once per keystroke, which is what keeps a
 * 200-attachment, workspace-with-hundreds-of-files draft under the
 * 16ms-per-keystroke budget instead of re-matching on every character.
 */

const MENTION = /(^|\s)@([^\s@]*)$/;
const SLASH_LINE = /^\/[a-z0-9?_-]*(?:\s+[a-z0-9?_-]*)?$/i;

export type SuggestionIntent =
  | { kind: 'mention'; query: string }
  | { kind: 'commands'; items: Suggestion[] }
  | { kind: 'none' };

export function resolveSuggestionIntent(value: string, caret: number): SuggestionIntent {
  const before = value.slice(0, caret);
  const m = MENTION.exec(before);
  if (m) return { kind: 'mention', query: m[2] };
  if (SLASH_LINE.test(value)) return { kind: 'commands', items: matchCommands(value) };
  return { kind: 'none' };
}

/**
 * A server bug, an older backend ignoring `limit=`, or a workspace with a
 * huge number of same-prefix files could hand the dropdown far more rows
 * than fit on screen. Reconciling hundreds of `<li>`s on every keystroke is
 * exactly the kind of frame-budget miss this lote measures against (200
 * attachments already on screen plus a mention list that should be a
 * handful of rows) — so the list actually rendered is capped here,
 * independent of whatever the server sent.
 */
export const MAX_MENTION_ITEMS = 20;

export function capMentionItems<T>(items: T[], max = MAX_MENTION_ITEMS): T[] {
  return items.length > max ? items.slice(0, max) : items;
}
