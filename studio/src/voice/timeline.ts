import { locale, t } from '../i18n';

/** Wall-clock instants (ms since the epoch) of one voice turn, in the order a
 *  healthy turn produces them. Same names the server stores
 *  (`src/voice_timeline.py`). */
export interface VoiceMarks {
  speech_end?: number;
  transcript_ready?: number;
  request_sent?: number;
  first_token?: number;
  tts_chunk?: number;
  first_audio?: number;
  /** How long the server spent on speech-to-text / text-to-speech (its own clock). */
  stt_server_ms?: number;
  tts_server_ms?: number;
}

export type GapName =
  | 'speech_to_text' | 'text_to_first_token' | 'first_token_to_audio' | 'text_to_request'
  | 'request_to_first_token' | 'first_token_to_tts_chunk' | 'tts_chunk_to_playback' | 'total';

const GAPS: Record<GapName, [keyof VoiceMarks, keyof VoiceMarks]> = {
  speech_to_text: ['speech_end', 'transcript_ready'],
  text_to_first_token: ['transcript_ready', 'first_token'],
  first_token_to_audio: ['first_token', 'first_audio'],
  text_to_request: ['transcript_ready', 'request_sent'],
  request_to_first_token: ['request_sent', 'first_token'],
  first_token_to_tts_chunk: ['first_token', 'tts_chunk'],
  tts_chunk_to_playback: ['tts_chunk', 'first_audio'],
  total: ['speech_end', 'first_audio'],
};
const MAX_GAP_MS = 10 * 60 * 1000;

/** Milliseconds between marks; null when an end is missing or the order is impossible. */
export function computeGaps(marks: VoiceMarks): Record<GapName, number | null> {
  const out = {} as Record<GapName, number | null>;
  for (const name of Object.keys(GAPS) as GapName[]) {
    const [a, b] = GAPS[name];
    const start = marks[a], end = marks[b];
    if (start == null || end == null) { out[name] = null; continue; }
    const delta = end - start;
    out[name] = delta >= 0 && delta <= MAX_GAP_MS ? Math.round(delta) : null;
  }
  return out;
}

/** "0,4 s" / "1.2 s" by interface language; one decimal, "-" when unknown. */
export function seconds(ms: number | null | undefined): string {
  if (ms == null) return '-';
  return `${(ms / 1000).toLocaleString(locale(), { minimumFractionDigits: 1, maximumFractionDigits: 1 })} s`;
}

/** The compact line shown on a voice turn. Only the gaps that exist appear. */
export function timelineLine(marks: VoiceMarks): string {
  const g = computeGaps(marks);
  const parts: string[] = [];
  if (g.speech_to_text != null) parts.push(`${t('speech→text')} ${seconds(g.speech_to_text)}`);
  if (g.text_to_first_token != null) parts.push(`${t('text→1st token')} ${seconds(g.text_to_first_token)}`);
  if (g.first_token_to_audio != null) parts.push(`${t('1st token→audio')} ${seconds(g.first_token_to_audio)}`);
  return parts.join(' · ');
}

/** The longer breakdown for the hover/title: every gap that exists, then the server's own timings. */
export function timelineDetail(marks: VoiceMarks): string {
  const g = computeGaps(marks);
  const rows: [string, number | null][] = [
    [t('Speech ends → text ready'), g.speech_to_text],
    [t('Text ready → request sent'), g.text_to_request],
    [t('Request sent → first token'), g.request_to_first_token],
    [t('First token → first speech chunk'), g.first_token_to_tts_chunk],
    [t('Speech chunk → playing'), g.tts_chunk_to_playback],
    [t('Total, speech ends → first audio'), g.total],
  ];
  const lines = rows.filter(([, v]) => v != null).map(([k, v]) => `${k}: ${seconds(v)}`);
  if (marks.stt_server_ms != null) lines.push(`${t('Server speech-to-text')}: ${seconds(marks.stt_server_ms)}`);
  if (marks.tts_server_ms != null) lines.push(`${t('Server text-to-speech')}: ${seconds(marks.tts_server_ms)}`);
  return lines.join('\n');
}

/** Fire-and-forget: a failed timeline must never cost a voice turn. */
export function postTimeline(sessionId: string, turnId: string, marks: VoiceMarks): void {
  if (!sessionId || !turnId) return;
  try {
    void fetch('/api/voice/timeline', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, turn_id: turnId, ...marks }),
      keepalive: true,
    }).catch(() => undefined);
  } catch { /* no fetch (tests, old browsers) */ }
}
