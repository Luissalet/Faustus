import { t, locale } from '../i18n';
/**
 * Dictation and read-aloud. The server has both (/api/stt, /api/tts:
 * local Whisper/Kokoro or an API); when it answers 503 it is set to
 * "browser mode", and the browser's own SpeechRecognition /
 * speechSynthesis take over — the same two-tier arrangement as
 * static/js/voice.js.
 */

type Recognition = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: ((e: { error?: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};

function browserRecognition(): (new () => Recognition) | null {
  const w = window as unknown as { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export interface Dictation {
  /** Resolves with the text when the recording ends (stop() or silence). */
  done: Promise<string>;
  stop: () => void;
}

/** Records from the microphone until stop(); transcribes on the server,
 *  or with the browser when the server is in browser mode. */
export async function startDictation(lang = locale()): Promise<Dictation> {
  // Server route: record with MediaRecorder, POST the blob.
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    throw new Error(t('No microphone available, or permission was not given.'));
  }
  const recorder = new MediaRecorder(stream);
  const chunks: Blob[] = [];
  recorder.ondataavailable = (e) => {
    if (e.data.size) chunks.push(e.data);
  };
  const done = new Promise<string>((resolve, reject) => {
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
      if (!blob.size) {
        resolve('');
        return;
      }
      const fd = new FormData();
      fd.append('file', blob, 'dictado.webm');
      try {
        const response = await fetch('/api/stt/transcribe', { method: 'POST', body: fd, credentials: 'same-origin' });
        if (response.status === 503) {
          resolve(await browserTranscribe(lang));
          return;
        }
        if (!response.ok) throw new Error(`stt responded ${response.status}`);
        const raw = (await response.json()) as { text?: string };
        resolve(raw.text ?? '');
      } catch (e) {
        reject(e as Error);
      }
    };
  });
  recorder.start();
  return { done, stop: () => recorder.state !== 'inactive' && recorder.stop() };
}

/** Browser mode: a short one-shot recognition (no server involved). */
function browserTranscribe(lang: string): Promise<string> {
  const Ctor = browserRecognition();
  if (!Ctor) return Promise.reject(new Error(t('This browser has no speech recognition and the server is in browser mode.')));
  return new Promise((resolve, reject) => {
    const rec = new Ctor();
    rec.lang = lang;
    rec.interimResults = false;
    rec.continuous = false;
    let text = '';
    rec.onresult = (e) => {
      text = Array.from({ length: e.results.length }, (_, i) => e.results[i][0]?.transcript ?? '').join(' ');
    };
    rec.onerror = (e) => reject(new Error(e.error ?? t('recognition failed')));
    rec.onend = () => resolve(text.trim());
    rec.start();
  });
}

let current: HTMLAudioElement | null = null;

/** Reads text aloud; returns a stop function. Server audio first, the
 *  browser's voices when the server is in browser mode. */
export async function speak(text: string): Promise<() => void> {
  stopSpeaking();
  const clean = text.replace(/```[\s\S]*?```/g, ` ${t('code')} `).replace(/[*_#>`]/g, '').slice(0, 4000);
  const response = await fetch('/api/tts/synthesize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ text: clean, format: 'audio' }),
  }).catch(() => null);
  if (response && response.ok) {
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    current = audio;
    audio.onended = () => {
      URL.revokeObjectURL(url);
      if (current === audio) current = null;
    };
    await audio.play();
    return () => {
      audio.pause();
      URL.revokeObjectURL(url);
      if (current === audio) current = null;
    };
  }
  if (!('speechSynthesis' in window)) throw new Error(t('No voice available: the server does not synthesise and neither does the browser.'));
  const utter = new SpeechSynthesisUtterance(clean);
  utter.lang = locale();
  window.speechSynthesis.speak(utter);
  return () => window.speechSynthesis.cancel();
}

export function stopSpeaking(): void {
  if (current) {
    current.pause();
    current = null;
  }
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
}
