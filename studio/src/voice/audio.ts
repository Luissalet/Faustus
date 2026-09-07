import { locale, t } from '../i18n';
import { TurnDetector, spokenText } from './engine';

export interface SpeechCapabilities {
  provider: string;
  configured: boolean;
  dependency_installed: boolean;
  execution: 'local' | 'browser' | 'endpoint' | 'disabled';
  model: string;
  language: string;
  voice: string;
}
export async function capabilities(kind: 'stt' | 'tts', signal?: AbortSignal): Promise<SpeechCapabilities> {
  const response = await fetch(`/api/${kind}/capabilities`, { credentials: 'same-origin', signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000) });
  if (!response.ok) throw new Error(t('Could not load voice settings. Refresh or check the server.'));
  return response.json();
}

export function audioLevel(analyser: AnalyserNode): number {
  const data = new Uint8Array(analyser.fftSize);
  analyser.getByteTimeDomainData(data);
  return Math.sqrt(data.reduce((sum, n) => sum + ((n - 128) / 128) ** 2, 0) / data.length);
}

type Recognition = {
  lang: string; interimResults: boolean; continuous: boolean;
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void; stop(): void; abort(): void;
};
function recognitionConstructor() {
  const w = window as unknown as { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition;
}
export interface Capture {
  readonly language?: string;
  done: Promise<string>;
  stop(): void;
  cancel(): void;
  analyser: AnalyserNode | null;
}
export interface CaptureOptions {
  signal: AbortSignal;
  deviceId?: string;
  autoStop?: boolean;
  lang?: string;
  onPartial?: (text: string) => void;
  onTranscribing?: () => void;
}
const aborted = () => new DOMException('Cancelled', 'AbortError');

export async function capture(config: SpeechCapabilities, options: CaptureOptions): Promise<Capture> {
  const { signal } = options;
  signal.throwIfAborted();
  if (!config.configured || !config.dependency_installed) throw new Error(t('Enable a speech recognition provider in Settings → Voice.'));
  if (config.execution === 'browser') {
    const Ctor = recognitionConstructor();
    if (!Ctor) throw new Error(t('This browser cannot transcribe speech. Choose local Whisper in Settings → Voice.'));
    const rec = new Ctor();
    rec.lang = options.lang && options.lang !== 'auto' ? options.lang : config.language || locale();
    rec.interimResults = true; rec.continuous = false;
    let resolve!: (text: string) => void, reject!: (e: Error) => void, text = '';
    const done = new Promise<string>((yes, no) => { resolve = yes; reject = no; });
    const cancel = () => { rec.abort(); reject(aborted()); };
    const timer = window.setTimeout(() => rec.stop(), 60000);
    const cleanup = () => { clearTimeout(timer); signal.removeEventListener('abort', cancel); };
    rec.onresult = e => {
      text = Array.from(e.results, r => r[0]?.transcript || '').join(' ').trim();
      options.onPartial?.(text);
    };
    rec.onerror = e => { cleanup(); reject(new Error(e.error)); };
    rec.onend = () => { cleanup(); signal.aborted ? reject(aborted()) : resolve(text); };
    signal.addEventListener('abort', cancel, { once: true });
    done.catch(() => undefined);
    try { rec.start(); } catch (e) { cleanup(); throw e; }
    return { done, stop: () => rec.stop(), cancel, analyser: null, language: rec.lang };
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) throw new Error(t('Microphone access needs HTTPS or localhost and a supported browser.'));
  const stream = await navigator.mediaDevices.getUserMedia({ audio: {
    echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    ...(options.deviceId ? { deviceId: { exact: options.deviceId } } : {}),
  } });
  if (signal.aborted) { stream.getTracks().forEach(t => t.stop()); throw aborted(); }
  let ctx: AudioContext | null = null;
  try {
    ctx = new AudioContext();
    await ctx.resume();
    signal.throwIfAborted();
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser(); analyser.fftSize = 512;
    source.connect(analyser);
    const mime = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'].find(x => MediaRecorder.isTypeSupported(x));
    const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    const chunks: Blob[] = [];
    let bytes = 0, resolve!: (s: string) => void, reject!: (e: Error) => void;
    const done = new Promise<string>((yes, no) => { resolve = yes; reject = no; });
    done.catch(() => undefined);
    const detector = new TurnDetector();
    const start = performance.now();
    let voiced = false, cancelled = false, detectedLanguage = '';
    const stop = () => { if (recorder.state !== 'inactive') recorder.stop(); };
    const cancel = () => { cancelled = true; stop(); reject(aborted()); cleanup(); };
    const cleanup = () => {
      clearInterval(timer); signal.removeEventListener('abort', cancel);
      stream.getTracks().forEach(t => { t.onended = null; t.stop(); }); source.disconnect();
      if (ctx?.state !== 'closed') void ctx?.close();
    };
    const timer = window.setInterval(() => {
      const level = audioLevel(analyser); if (level > 0.025) voiced = true;
      if ((options.autoStop && detector.push(level, performance.now()) !== 'continue') || performance.now() - start > 60000) stop();
    }, 50);
    recorder.ondataavailable = e => {
      bytes += e.data.size;
      if (bytes > 16 * 1024 * 1024) { reject(new Error(t('Recording is too large. Try a shorter message.'))); cancel(); }
      else if (e.data.size) chunks.push(e.data);
    };
    recorder.onerror = () => { reject(new Error(t('Recording failed. Reconnect your microphone and retry.'))); cancel(); };
    for (const track of stream.getTracks()) track.onended = () => { reject(new Error(t('Microphone disconnected. Reconnect it and retry.'))); cancel(); };
    recorder.onstop = async () => {
      cleanup();
      if (signal.aborted || cancelled) { reject(aborted()); return; }
      if (!voiced || !bytes) { resolve(''); return; }
      options.onTranscribing?.();
      const fd = new FormData();
      fd.append('file', new Blob(chunks, { type: recorder.mimeType }), recorder.mimeType.includes('mp4') ? 'voice.mp4' : 'voice.webm');
      fd.append('expected_provider', config.provider);
      if (options.lang) fd.append('language', options.lang);
      try {
        const response = await fetch('/api/stt/transcribe', { method: 'POST', body: fd, credentials: 'same-origin', signal: AbortSignal.any([signal, AbortSignal.timeout(120000)]) });
        if (!response.ok) throw new Error(t('Transcription failed. Check Settings → Voice and try again.'));
        const result = await response.json() as { text?: string; language?: string };
        detectedLanguage = result.language || (options.lang !== 'auto' ? options.lang : '') || config.language;
        signal.throwIfAborted(); resolve(result.text?.trim() || '');
      } catch (e) { reject(e as Error); }
    };
    signal.addEventListener('abort', cancel, { once: true });
    recorder.start(250);
    return { done, stop, cancel, analyser, get language() { return detectedLanguage; } };
  } catch (e) {
    stream.getTracks().forEach(t => t.stop());
    if (ctx?.state !== 'closed') void ctx?.close();
    throw e;
  }
}

/** Resolves at playback END. Abort stops audio and invalidates pending synthesis. */
let outputLease: AbortController | null = null;
export async function playSpeech(text: string, config: SpeechCapabilities, signal: AbortSignal, onAudio?: (analyser: AnalyserNode | null, playing: boolean) => void): Promise<void> {
  outputLease?.abort();
  const lease = new AbortController(); outputLease = lease;
  try {
    await playSegment(text, config, AbortSignal.any([signal, lease.signal]), onAudio);
  } finally { if (outputLease === lease) outputLease = null; }
}

async function playSegment(text: string, config: SpeechCapabilities, signal: AbortSignal, onAudio?: (analyser: AnalyserNode | null, playing: boolean) => void): Promise<void> {
  signal.throwIfAborted();
  const clean = spokenText(text).slice(0, 5000);
  if (!clean) return;
  if (!config.configured || !config.dependency_installed) throw new Error(t('Enable a speech output provider in Settings → Voice.'));
  if (config.execution === 'browser') {
    if (!window.speechSynthesis) throw new Error(t('This browser has no speech output.'));
    await new Promise<void>((resolve, reject) => {
      const utter = new SpeechSynthesisUtterance(clean); utter.lang = config.language || locale();
      // Only installed device voices: never silently send response text to a cloud voice.
      const voices = speechSynthesis.getVoices().filter(v => v.localService);
      const voice = voices.find(v => v.lang.startsWith(utter.lang.split('-')[0]));
      if (!voice) { reject(new Error(t('No local browser voice for this language. Configure a speech output provider.'))); return; }
      utter.voice = voice;
      const cancel = () => { speechSynthesis.cancel(); finish(aborted()); };
      const finish = (error?: Error) => { clearTimeout(timer); signal.removeEventListener('abort', cancel); onAudio?.(null, false); error ? reject(error) : resolve(); };
      const timer = window.setTimeout(() => { speechSynthesis.cancel(); finish(new Error(t('Speech playback timed out.'))); }, 120000);
      signal.addEventListener('abort', cancel, { once: true });
      utter.onstart = () => onAudio?.(null, true);
      utter.onend = () => finish(); utter.onerror = e => finish(new Error(e.error));
      speechSynthesis.speak(utter);
    });
    return;
  }
  const response = await fetch('/api/tts/synthesize', {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: clean, format: 'audio', use_cache: false, expected_provider: config.provider, language: config.language || locale() }),
    signal: AbortSignal.any([signal, AbortSignal.timeout(120000)]),
  });
  if (!response.ok) throw new Error(t('Speech output failed. The full response is still in the chat.'));
  const blob = await response.blob(); signal.throwIfAborted();
  const url = URL.createObjectURL(blob), audio = new Audio(url);
  let ctx: AudioContext | null = null;
  try {
    ctx = new AudioContext(); await ctx.resume(); signal.throwIfAborted();
    const source = ctx.createMediaElementSource(audio), analyser = ctx.createAnalyser();
    analyser.fftSize = 512; source.connect(analyser); analyser.connect(ctx.destination);
    await new Promise<void>((resolve, reject) => {
      const finish = (error?: Error) => { clearTimeout(timer); signal.removeEventListener('abort', cancel); error ? reject(error) : resolve(); };
      const cancel = () => { audio.pause(); finish(aborted()); };
      const timer = window.setTimeout(() => { audio.pause(); finish(new Error(t('Speech playback timed out.'))); }, 120000);
      signal.addEventListener('abort', cancel, { once: true });
      audio.onended = () => finish(); audio.onerror = () => finish(new Error(t('Could not play speech audio.')));
      audio.onplaying = () => onAudio?.(analyser, true);
      void audio.play().catch(e => finish(e));
    });
  } finally {
    audio.pause(); audio.removeAttribute('src'); audio.load(); URL.revokeObjectURL(url);
    if (ctx?.state !== 'closed') void ctx?.close();
    onAudio?.(null, false);
  }
}
