/** Pure, bounded policies shared by the voice UI and regression checks. */
export type VoicePhase = 'idle' | 'starting' | 'listening' | 'transcribing' | 'review' | 'thinking' | 'speaking' | 'approval' | 'error';

/** Lightweight output-voice selection, not an instruction to the model. */
export function speechLanguage(text: string, fallback = 'en'): 'es' | 'en' {
  const words = new Set(spokenText(text).toLowerCase().match(/[a-záéíóúüñ]+/g) || []);
  const score = (list: string[]) => list.filter(w => words.has(w)).length;
  const es = score(['hola', 'gracias', 'puedes', 'tienes', 'quieres', 'para', 'una', 'está', 'están', 'los', 'las', 'del', 'esto', 'aquí', 'he', 'con', 'que', 'pero', 'como', 'español']);
  const en = score(['hello', 'thanks', 'please', 'you', 'your', 'the', 'this', 'that', 'with', 'have', 'has', 'can', 'will', 'are', 'and', 'but', 'from', 'here', 'english']);
  if (words.size <= 3 && (words.has('hello') || words.has('thanks'))) return 'en';
  if (words.size <= 3 && (words.has('hola') || words.has('gracias'))) return 'es';
  if (es !== en && Math.max(es, en) >= 2) return es > en ? 'es' : 'en';
  return fallback.startsWith('es') ? 'es' : 'en';
}

export class TurnDetector {
  private started = 0;
  private voiced = 0;
  private lastVoice = 0;
  // 900ms is the "pause that ends your turn" default (was 1200ms): fast
  // enough to feel like a conversation, still longer than a normal breath.
  // The panel exposes short/normal/long as a user preference (see
  // VoicePanel's `silenceMs` prefs key); this constructor default is the
  // fallback when no preference is read yet.
  constructor(readonly silenceMs = 900, readonly maximumMs = 60000) {}
  push(level: number, now: number): 'continue' | 'silence' | 'limit' | 'empty' {
    if (!this.started) this.started = now;
    if (level > 0.025) { this.voiced += 50; this.lastVoice = now; }
    if (now - this.started >= this.maximumMs) return 'limit';
    if (this.voiced >= 250 && now - this.lastVoice > this.silenceMs) return 'silence';
    if (!this.voiced && now - this.started > 15000) return 'empty';
    return 'continue';
  }
}

// Higher than the 0.025 VAD threshold above: while Faustus is speaking, the
// speaker's own audio can bleed into the microphone even with echo
// cancellation on, so barge-in needs a stronger signal before it cuts the
// reply off. Sustained for BARGE_IN_MS to reject a single loud click/breath.
export const BARGE_IN_THRESHOLD = 0.05;
export const BARGE_IN_MS = 250;

/** lowercase, strip accents and punctuation, collapse whitespace. */
export function normalizeUtterance(text: string): string {
  return text
    .toLowerCase()
    .normalize('NFD').replace(/[̀-ͯ]/g, '')
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Token-overlap similarity (Dice coefficient), 0 (nothing shared) to 1 (identical bags of words). */
export function similarity(a: string, b: string): number {
  const ta = normalizeUtterance(a).split(' ').filter(Boolean);
  const tb = normalizeUtterance(b).split(' ').filter(Boolean);
  if (!ta.length && !tb.length) return 1;
  if (!ta.length || !tb.length) return 0;
  const remaining = new Map<string, number>();
  for (const w of tb) remaining.set(w, (remaining.get(w) || 0) + 1);
  let overlap = 0;
  for (const w of ta) {
    const count = remaining.get(w) || 0;
    if (count > 0) { overlap++; remaining.set(w, count - 1); }
  }
  return (2 * overlap) / (ta.length + tb.length);
}

/** Whole-utterance, case/punctuation-insensitive: "stop it now" is not a stop phrase, "stop" and "¡para!" are. */
export const STOP_PHRASES = ['stop', 'para', 'cállate', 'callate', 'silencio', 'espera', 'shut up', 'wait'];
export function isStopPhrase(text: string): boolean {
  const clean = normalizeUtterance(text);
  return clean.length > 0 && STOP_PHRASES.some(p => normalizeUtterance(p) === clean);
}

/** Whisper's well-known silence/no-speech hallucinations, and anything too short to be real. */
export const HALLUCINATIONS = ['you', 'thank you', 'thanks for watching', 'gracias', 'subtítulos'];
export function isHallucination(text: string): boolean {
  const clean = normalizeUtterance(text);
  if (clean.length <= 2) return true;
  if (HALLUCINATIONS.some(p => normalizeUtterance(p) === clean)) return true;
  // "Subtítulos realizados por la comunidad de Amara.org" and its many
  // variants: a whole-utterance hallucination that always starts this way.
  return clean.startsWith('subtitulos');
}

/** true when a just-heard utterance is really the tail of what Faustus itself was saying (mic re-heard the speaker). */
export function isEcho(heard: string, recentlySpoken: string, thresholdMs: number, msSinceSpeechStarted: number): boolean {
  if (msSinceSpeechStarted > thresholdMs) return false;
  return similarity(heard, recentlySpoken) >= 0.8;
}

const WAKE_WORDS = ['faustus', 'faustos', 'fausto', 'faust'];
/** Strips a leading wake word (within the first 3 words) when present; `matched` tells the caller whether to forward the rest. */
export function stripWakeWord(text: string): { matched: boolean; text: string } {
  const words = text.trim().split(/\s+/).filter(Boolean);
  for (let i = 0; i < Math.min(3, words.length); i++) {
    const clean = normalizeUtterance(words[i]);
    if (clean === 'faust' && words[i + 1] && normalizeUtterance(words[i + 1]) === 'us') {
      return { matched: true, text: [...words.slice(0, i), ...words.slice(i + 2)].join(' ').trim() };
    }
    if (WAKE_WORDS.includes(clean)) {
      return { matched: true, text: [...words.slice(0, i), ...words.slice(i + 1)].join(' ').trim() };
    }
  }
  return { matched: false, text: text.trim() };
}

export function spokenText(text: string): string {
  // An unfinished code fence is omitted too: never pronounce streaming code.
  return text.split('```').filter((_, i) => i % 2 === 0).join(' ')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/https?:\/\/\S+/g, '')
    .replace(/^\s*\|.*$/gm, '')
    .replace(/[*_#>`]/g, '').replace(/\s+/g, ' ').trim();
}

/** Feed cumulative assistant text, never reasoning/tool output. */
export class SentenceBuffer {
  private cursor = 0;
  private spent = 0;
  truncated = false;
  constructor(readonly budget = 4000) {}
  take(text: string, final = false): string[] {
    const clean = spokenText(text);
    const out: string[] = [];
    while (this.cursor < clean.length && !this.truncated) {
      const remaining = clean.slice(this.cursor).trimStart();
      let end = -1;
      const punctuation = /[.!?。！？](?:\s|$)/g;
      for (const match of remaining.matchAll(punctuation)) {
        const at = match.index! + 1;
        if (!final && at === remaining.length) break; // token may still be incomplete
        if (/\b(?:Dr|Mr|Mrs|Sr|Sra|etc|vs)\.$/i.test(remaining.slice(0, at))) continue;
        end = at; break;
      }
      if (end < 0 && remaining.length > 320) end = remaining.lastIndexOf(' ', 280);
      if (end < 0 && final) end = remaining.length;
      if (end < 0) break;
      const part = remaining.slice(0, end).trim();
      if (this.spent + part.length > this.budget) { this.truncated = true; break; }
      this.cursor = clean.length - remaining.length + end;
      this.spent += part.length;
      if (part) out.push(part);
    }
    return out;
  }
}
