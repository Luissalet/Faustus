/**
 * A11Y-02 — screen readers and streaming: deciding whether it is time to
 * announce new streamed text, and what the new chunk is. Pure (no React, no
 * timers, no DOM) so it unit-tests as a function of the clock — a long
 * stream must never turn into hundreds of announcements, one per token,
 * that would bury the one thing a screen reader user actually needs to keep
 * hearing while it streams: the Stop button's own state.
 *
 * Used by `screens/studio/Transcript.tsx`'s `useGroupedStreamAnnouncement`,
 * which feeds a `polite` live region — never the whole growing message,
 * only the new chunk each time an announcement is due.
 */
export interface StreamAnnouncement {
  /** Just the new text since the last announcement — not the whole message. */
  chunk: string;
  /** How much of `fullText` this announcement covers; the next call's
   *  `announcedLength`. */
  length: number;
  /** This announcement's timestamp; the next call's `lastAnnouncedAt`. */
  at: number;
}

export function nextStreamAnnouncement(
  announcedLength: number,
  fullText: string,
  lastAnnouncedAt: number,
  now: number,
  minIntervalMs = 2000,
): StreamAnnouncement | null {
  if (fullText.length <= announcedLength) return null;
  if (now - lastAnnouncedAt < minIntervalMs) return null;
  return { chunk: fullText.slice(announcedLength), length: fullText.length, at: now };
}
