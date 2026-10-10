/* Word timings and visemes of the speech Faustus is playing (Agora #251).
 *
 * The server sends, next to the audio, when each word starts and ends and which
 * mouth shape each stretch of sound has (docs/api/tts-marks.md). This file is the
 * pure part of using them: reading and checking that data, finding what is being
 * said at a given moment of the audio, easing a mouth shape from one viseme to
 * the next, and cutting the subtitle into pieces so the current word can light
 * up. Nothing here touches the DOM, so tests/voice_visemes.check.mjs runs it. */

export type Viseme = 'rest' | 'AA' | 'E' | 'I' | 'O' | 'U' | 'MBP' | 'FV' | 'L' | 'OTHER';
export const VISEMES: readonly Viseme[] = ['rest', 'AA', 'E', 'I', 'O', 'U', 'MBP', 'FV', 'L', 'OTHER'];

export interface MarkWord { i: number; text: string; startChar: number; endChar: number; startMs: number; endMs: number }
export interface MarkViseme { v: Viseme; startMs: number; endMs: number }
export interface SpeechMarks {
  durationMs: number;
  timing: 'audio' | 'text_only';
  wordsSource: 'engine' | 'estimated';
  visemesSource: 'engine' | 'estimated';
  words: MarkWord[];
  visemes: MarkViseme[];
}

/** What a player hands to the orb and the subtitle: the text that is being spoken, its marks (if the server sent
 *  any) and the position of the audio in ms, already corrected for output latency. */
export interface Playback { text: string; marks: SpeechMarks | null; currentMs(): number }

/** How long the word just spoken stays lit when nothing follows it right away. */
export const WORD_HOLD_MS = 350;

const isNum = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x);

/** Marks from the API response, or null when they are missing or do not describe `text`: a subtitle that lights the
 *  wrong word is worse than one that lights none, so anything inconsistent is dropped whole. */
export function parseMarks(raw: unknown, text: string): SpeechMarks | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  if (r.version !== 1 || !isNum(r.duration_ms) || !Array.isArray(r.words) || !Array.isArray(r.visemes)) return null;
  const words: MarkWord[] = [];
  let lastEnd = 0, lastChar = 0;
  for (const w of r.words as Record<string, unknown>[]) {
    if (!w || typeof w.text !== 'string' || !isNum(w.start_ms) || !isNum(w.end_ms) || !isNum(w.start_char) || !isNum(w.end_char)) return null;
    if (w.start_ms < lastEnd || w.end_ms < w.start_ms || w.start_char < lastChar || w.end_char <= w.start_char) return null;
    if (text.slice(w.start_char, w.end_char) !== w.text) return null;
    words.push({ i: words.length, text: w.text, startChar: w.start_char, endChar: w.end_char, startMs: w.start_ms, endMs: w.end_ms });
    lastEnd = w.end_ms; lastChar = w.end_char;
  }
  const visemes: MarkViseme[] = [];
  let last = 0;
  for (const v of r.visemes as Record<string, unknown>[]) {
    if (!v || !VISEMES.includes(v.v as Viseme) || !isNum(v.start_ms) || !isNum(v.end_ms) || v.start_ms < last || v.end_ms <= v.start_ms) return null;
    visemes.push({ v: v.v as Viseme, startMs: v.start_ms, endMs: v.end_ms });
    last = v.end_ms;
  }
  return {
    durationMs: r.duration_ms, timing: r.timing === 'text_only' ? 'text_only' : 'audio',
    wordsSource: r.words_source === 'engine' ? 'engine' : 'estimated', visemesSource: r.visemes_source === 'engine' ? 'engine' : 'estimated',
    words, visemes,
  };
}

/** Index of the last item whose start is <= ms, or -1 (items sorted by start). */
function lastStartedAt<T>(items: readonly T[], start: (t: T) => number, ms: number): number {
  let lo = 0, hi = items.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (start(items[mid]) <= ms) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return found;
}

/** The word being said at `ms`: the last one that started, kept lit through the pause after it for up to WORD_HOLD_MS. -1: none. */
export function wordAt(marks: SpeechMarks, ms: number): number {
  const k = lastStartedAt(marks.words, w => w.startMs, ms);
  if (k < 0) return -1;
  const w = marks.words[k], next = marks.words[k + 1];
  if (ms < w.endMs) return k;
  return ms < w.endMs + WORD_HOLD_MS && (!next || ms < next.startMs) ? k : -1;
}

/** The mouth shape of the sound at `ms`; a closed mouth anywhere the marks say nothing. */
export function visemeAt(marks: SpeechMarks | null, ms: number): Viseme {
  if (!marks) return 'rest';
  const k = lastStartedAt(marks.visemes, v => v.startMs, ms);
  return k >= 0 && ms < marks.visemes[k].endMs ? marks.visemes[k].v : 'rest';
}

/** A mouth as four independent amounts, each 0..1, so that easing between two shapes is just easing four numbers. */
export interface Mouth { open: number; wide: number; round: number; press: number }

export const MOUTHS: Record<Viseme, Mouth> = {
  rest: { open: 0, wide: 0, round: 0, press: 0 },
  AA: { open: 1, wide: 0.3, round: 0, press: 0 },
  E: { open: 0.5, wide: 0.8, round: 0, press: 0 },
  I: { open: 0.22, wide: 1, round: 0, press: 0 },
  O: { open: 0.75, wide: 0, round: 0.9, press: 0 },
  U: { open: 0.3, wide: 0, round: 1, press: 0 },
  MBP: { open: 0, wide: 0, round: 0, press: 1 },
  FV: { open: 0.15, wide: 0.4, round: 0, press: 0.55 },
  L: { open: 0.5, wide: 0.35, round: 0, press: 0 },
  OTHER: { open: 0.3, wide: 0.3, round: 0.1, press: 0 },
};

/** Ease `current` toward `target`. Frame-rate independent: after `tauMs` it has covered 63% of the way, so the orb
 *  looks the same at 30 fps and at 5 fps, and a viseme that lasts 50 ms still shows (the target moves on before it settles). */
export function stepMouth(current: Mouth, target: Mouth, dtMs: number, tauMs = 55): Mouth {
  const a = 1 - Math.exp(-Math.max(0, dtMs) / Math.max(1, tauMs));
  return {
    open: current.open + (target.open - current.open) * a, wide: current.wide + (target.wide - current.wide) * a,
    round: current.round + (target.round - current.round) * a, press: current.press + (target.press - current.press) * a,
  };
}

export interface OrbShape {
  /** Horizontal and vertical stretch of the sphere (1 = unchanged). */
  sx: number; sy: number;
  /** The ring around the sphere is its mouth: half-axes in units of the radius. */
  ringX: number; ringY: number;
}

/** How the orb is drawn for a mouth. With reduced motion nothing deforms: the orb keeps its round shape and only the
 *  subtitle follows the words. */
export function orbShape(m: Mouth, reducedMotion: boolean): OrbShape {
  if (reducedMotion) return { sx: 1, sy: 1, ringX: 1.28, ringY: 0.3 };
  return {
    sx: 1 + 0.09 * m.wide - 0.07 * m.round + 0.03 * m.press,
    sy: 1 + 0.13 * m.open + 0.04 * m.round - 0.1 * m.press - 0.02 * m.wide,
    ringX: 1.28 + 0.14 * m.wide - 0.16 * m.round,
    ringY: Math.max(0.06, 0.3 + 0.3 * m.open - 0.2 * m.press),
  };
}

export interface SubtitlePart { text: string; /** Index into `marks.words`, or null for the text between words. */ word: number | null }

/** `text` cut at the words of `marks`; the parts joined are exactly `text`. Without marks, one part. */
export function subtitleParts(text: string, marks: SpeechMarks | null): SubtitlePart[] {
  if (!marks || !marks.words.length) return text ? [{ text, word: null }] : [];
  const parts: SubtitlePart[] = [];
  let cursor = 0;
  for (const w of marks.words) {
    if (w.startChar > cursor) parts.push({ text: text.slice(cursor, w.startChar), word: null });
    parts.push({ text: text.slice(w.startChar, w.endChar), word: w.i });
    cursor = w.endChar;
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor), word: null });
  return parts;
}

/** The position of an audio element in ms as the listener hears it: `currentTime` runs ahead of the speakers by the
 *  output latency of the audio device (capped, since some browsers report nonsense). */
export function heardMs(currentTimeSeconds: number, outputLatencySeconds: number | undefined): number {
  const latency = Math.min(0.3, Math.max(0, outputLatencySeconds || 0));
  return Math.max(0, (currentTimeSeconds - latency) * 1000);
}
