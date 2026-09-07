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
  constructor(readonly silenceMs = 1200, readonly maximumMs = 60000) {}
  push(level: number, now: number): 'continue' | 'silence' | 'limit' | 'empty' {
    if (!this.started) this.started = now;
    if (level > 0.025) { this.voiced += 50; this.lastVoice = now; }
    if (now - this.started >= this.maximumMs) return 'limit';
    if (this.voiced >= 250 && now - this.lastVoice > this.silenceMs) return 'silence';
    if (!this.voiced && now - this.started > 15000) return 'empty';
    return 'continue';
  }
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
