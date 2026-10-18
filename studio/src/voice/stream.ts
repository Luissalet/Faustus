/**
 * Streaming voice session client (server side: services/voice_stream.py, /api/voice).
 *
 * The microphone is tapped continuously as 16 kHz PCM with a short pre-roll, so
 * a barge-in keeps its first syllable; utterances travel over one WebSocket and
 * come back as partial/final transcripts. Speech arrives already synthesized,
 * sentence by sentence, while the agent is still writing: the next sentence is
 * ready before the current one ends. Interrupting stops the voice only.
 */
import { t } from '../i18n';
import { audioLevel, type Capture, type CaptureOptions, type OpenMic } from './audio';
import { TurnDetector } from './engine';
import { heardMs } from './visemes';

export interface VoiceStreamCapabilities {
  available: boolean;
  speech_output: boolean;
  pcm_sample_rate: number;
  max_preroll_ms: number;
  partial_transcripts: boolean;
}

export async function voiceStreamCapabilities(signal?: AbortSignal): Promise<VoiceStreamCapabilities | null> {
  try {
    const response = await fetch('/api/voice/capabilities', {
      credentials: 'same-origin',
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
    });
    if (!response.ok) return null;
    const body = await response.json() as { streaming?: VoiceStreamCapabilities };
    return body.streaming ?? null;
  } catch { return null; }
}

export interface VoiceStreamEvent {
  seq: number;
  type: string;
  [key: string]: unknown;
}
export interface StreamAudio { seq: number; runId: string; text: string; language: string; mime: string; synthMs: number; blob: Blob; receivedAt: number }

interface OpenOptions { language: string; readAloud: boolean; partials: boolean; languageHint?: string }

/** One voice session bound to a chat, with its WebSocket. Reconnects resume from the last event seen. */
export class VoiceStream {
  private ws: WebSocket | null = null;
  private lastSeq = 0;
  private pendingAudio: VoiceStreamEvent | null = null;
  private closed = false;
  private listeners = new Set<(event: VoiceStreamEvent) => void>();
  private audioListeners = new Set<(audio: StreamAudio) => void>();
  private retry = 0;

  private constructor(readonly id: string, private readonly token: string, readonly sessionId: string) {}

  static async open(sessionId: string, options: OpenOptions, signal?: AbortSignal): Promise<VoiceStream> {
    const hint = options.languageHint?.split('-')[0];
    const response = await fetch('/api/voice/sessions', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, language: options.language, read_aloud: options.readAloud,
        partials: options.partials, send_mode: 'client', language_hint: hint === 'es' || hint === 'en' ? hint : '' }),
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error(t('Could not open the voice stream.'));
    const body = await response.json() as { id: string; token: string };
    const stream = new VoiceStream(body.id, body.token, sessionId);
    await stream.connect();
    return stream;
  }

  private connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${proto}//${location.host}/api/voice/sessions/${this.id}/stream?token=${encodeURIComponent(this.token)}&after=${this.lastSeq}`);
      ws.binaryType = 'arraybuffer';
      let opened = false;
      ws.onopen = () => { opened = true; this.retry = 0; resolve(); };
      ws.onerror = () => { if (!opened) reject(new Error(t('Could not open the voice stream.'))); };
      ws.onclose = () => {
        if (this.ws === ws) this.ws = null;
        if (!this.closed && opened && this.retry < 5) {
          this.retry += 1;
          window.setTimeout(() => { if (!this.closed) void this.connect().catch(() => undefined); }, 400 * this.retry);
        }
      };
      ws.onmessage = message => this.receive(message.data);
      this.ws = ws;
    });
  }

  private receive(data: unknown): void {
    if (data instanceof ArrayBuffer) {
      const meta = this.pendingAudio;
      this.pendingAudio = null;
      if (!meta) return;
      const audio: StreamAudio = {
        seq: meta.seq, runId: String(meta.run_id || ''), text: String(meta.text || ''), language: String(meta.language || ''),
        mime: String(meta.mime || 'audio/wav'), synthMs: Number(meta.synth_ms || 0),
        blob: new Blob([data], { type: String(meta.mime || 'audio/wav') }), receivedAt: Date.now(),
      };
      this.audioListeners.forEach(fn => fn(audio));
      return;
    }
    let event: VoiceStreamEvent;
    try { event = JSON.parse(String(data)); } catch { return; }
    if (typeof event.seq === 'number') this.lastSeq = Math.max(this.lastSeq, event.seq);
    if (event.type === 'audio' && typeof event.audio_bytes === 'number') { this.pendingAudio = event; return; }
    this.listeners.forEach(fn => fn(event));
  }

  onEvent(fn: (event: VoiceStreamEvent) => void): () => void { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  onAudio(fn: (audio: StreamAudio) => void): () => void { this.audioListeners.add(fn); return () => this.audioListeners.delete(fn); }
  get connected(): boolean { return !!this.ws && this.ws.readyState === WebSocket.OPEN; }

  send(message: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(message));
  }
  sendPcm(samples: Int16Array): void {
    if (this.ws?.readyState === WebSocket.OPEN && samples.length) this.ws.send(samples.buffer.slice(samples.byteOffset, samples.byteOffset + samples.byteLength));
  }
  interrupt(reason = 'user'): void { this.send({ type: 'interrupt', reason }); }
  played(runId: string): void { this.send({ type: 'played', run_id: runId, at: Date.now() / 1000 }); }
  options(patch: Partial<{ read_aloud: boolean; language: string; partials: boolean }>): void { this.send({ type: 'options', ...patch }); }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.send({ type: 'stop' });
    try { this.ws?.close(); } catch { /* already closed */ }
    this.ws = null;
    void fetch(`/api/voice/sessions/${this.id}`, { method: 'DELETE', credentials: 'same-origin', keepalive: true }).catch(() => undefined);
  }
}

const TARGET_RATE = 16000;

/** Continuous 16 kHz PCM16 tap on the hot microphone, keeping the last `prerollMs` for the next utterance. */
export class PcmTap {
  private node: ScriptProcessorNode;
  private mute: GainNode;
  private ring: Int16Array[] = [];
  private ringSamples = 0;
  private sink: ((samples: Int16Array) => void) | null = null;
  private pos = 0;
  private own: AudioContext | null = null;
  readonly maxPreroll: number;

  constructor(mic: OpenMic, prerollMs = 700) {
    this.maxPreroll = Math.round(TARGET_RATE * prerollMs / 1000);
    // A 16 kHz context of its own lets the browser's resampler (properly filtered) do the conversion;
    // where a browser cannot mix sample rates on one microphone, decimate the shared context here instead.
    let ctx: AudioContext = mic.ctx;
    let source: MediaStreamAudioSourceNode;
    try {
      const own = new AudioContext({ sampleRate: TARGET_RATE });
      try { source = own.createMediaStreamSource(mic.stream); } catch (e) { void own.close(); throw e; }
      void own.resume();
      this.own = own; ctx = own;
    } catch {
      source = ctx.createMediaStreamSource(mic.stream);
    }
    this.node = ctx.createScriptProcessor(2048, 1, 1);
    this.mute = ctx.createGain(); this.mute.gain.value = 0;
    source.connect(this.node); this.node.connect(this.mute); this.mute.connect(ctx.destination);
    const ratio = ctx.sampleRate / TARGET_RATE;
    this.node.onaudioprocess = event => {
      const input = event.inputBuffer.getChannelData(0);
      const out = new Int16Array(Math.ceil(input.length / ratio) + 1);
      let n = 0;
      // Box-filter decimation; the fractional read position carries across buffers.
      for (; this.pos < input.length; this.pos += ratio) {
        const first = Math.floor(this.pos), last = Math.min(input.length, Math.max(first + 1, Math.floor(this.pos + ratio)));
        let sum = 0;
        for (let i = first; i < last; i++) sum += input[i];
        const v = Math.max(-1, Math.min(1, sum / (last - first)));
        out[n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      }
      this.pos -= input.length;
      const frame = out.slice(0, n);
      if (this.sink) { this.sink(frame); return; }
      this.ring.push(frame); this.ringSamples += frame.length;
      while (this.ring.length && this.ringSamples - this.ring[0].length >= this.maxPreroll) {
        this.ringSamples -= this.ring.shift()!.length;
      }
    };
  }

  /** Milliseconds of audio held for the next utterance. */
  get bufferedMs(): number { return Math.round(this.ringSamples / TARGET_RATE * 1000); }
  /** Start forwarding: the pre-roll first, then live frames. */
  begin(sink: (samples: Int16Array) => void): void {
    const preroll = this.ring;
    this.ring = []; this.ringSamples = 0;
    for (const frame of preroll) sink(frame);
    this.sink = sink;
  }
  end(): void { this.sink = null; }
  close(): void {
    this.sink = null; this.node.onaudioprocess = null;
    try { this.node.disconnect(); this.mute.disconnect(); } catch { /* already gone */ }
    if (this.own && this.own.state !== 'closed') void this.own.close();
    this.own = null;
  }
}

/** Same contract as `capture` from ./audio, over the voice stream: turn detection here, transcription on the server. */
export function streamCapture(stream: VoiceStream, tap: PcmTap, options: CaptureOptions): Promise<Capture> {
  const { signal } = options;
  signal.throwIfAborted();
  const mic = options.mic;
  if (!mic) throw new Error(t('Microphone access needs HTTPS or localhost and a supported browser.'));
  const detector = new TurnDetector(options.silenceMs);
  let resolve!: (text: string) => void, reject!: (e: Error) => void;
  const done = new Promise<string>((yes, no) => { resolve = yes; reject = no; });
  done.catch(() => undefined);
  let language = '', rawText = '', sttServerMs: number | undefined;
  let ended = false, finished = false, voiced = false;
  const start = performance.now();
  stream.send({ type: 'utterance_start', sample_rate: TARGET_RATE, preroll_ms: tap.bufferedMs,
    language_hint: options.languageHint?.split('-')[0] || '' });
  tap.begin(samples => stream.sendPcm(samples));
  const off = stream.onEvent(event => {
    if (event.type !== 'transcript') return;
    if (!event.final) { if (!ended) options.onPartial?.(String(event.text || '')); return; }
    if (!ended || finished) return;
    finished = true; cleanup();
    language = String(event.language || '');
    rawText = String(event.raw ?? event.text ?? '');
    sttServerMs = typeof event.stt_ms === 'number' ? event.stt_ms : undefined;
    resolve(event.verdict === 'empty' ? '' : rawText.trim());
  });
  const finish = () => {
    if (ended) return;
    ended = true; tap.end();
    if (!voiced) { finished = true; cleanup(); stream.send({ type: 'utterance_cancel' }); resolve(''); return; }
    options.onTranscribing?.();
    stream.send({ type: 'utterance_end', speech_end_at: Date.now() / 1000 });
  };
  // Speculative transcript (same policy as SpeculativeTranscript in ./engine):
  // after a short pause the server transcribes what it has; speaking again withdraws it.
  const eager = options.autoStop ? options.eagerMs ?? 350 : 0;
  let speculatedAt = 0, speculations = 0;
  const timer = window.setInterval(() => {
    if (ended) return;
    const now = performance.now();
    const level = audioLevel(mic.analyser);
    if (level > 0.025) {
      voiced = true;
      if (speculatedAt) { stream.send({ type: 'speculate_cancel' }); speculatedAt = 0; }
    }
    if ((options.autoStop && detector.push(level, now) !== 'continue') || now - start > 60000) { finish(); return; }
    const quiet = now - detector.lastVoiceAt;
    if (eager > 0 && !speculatedAt && speculations < 3 && detector.heard && detector.lastVoiceAt && quiet >= eager && quiet < detector.silenceMs) {
      stream.send({ type: 'speculate' }); speculatedAt = now; speculations += 1;
    }
  }, 50);
  const watchdog = window.setTimeout(() => { if (!finished) { finished = true; cleanup(); reject(new Error(t('Transcription failed. Check Settings → Voice and try again.'))); } }, 180000);
  const cancel = () => {
    if (!finished) { finished = true; ended = true; tap.end(); stream.send({ type: 'utterance_cancel' }); cleanup(); reject(new DOMException('Cancelled', 'AbortError')); }
  };
  function cleanup() { clearInterval(timer); clearTimeout(watchdog); off(); signal.removeEventListener('abort', cancel); }
  signal.addEventListener('abort', cancel, { once: true });
  return Promise.resolve({
    done, stop: finish, cancel, analyser: mic.analyser,
    get language() { return language; }, get rawText() { return rawText; }, get sttServerMs() { return sttServerMs; },
  });
}

/** Plays streamed sentences in order; `clear` stops the current one and drops the rest. */
export class StreamPlayer {
  private queue: StreamAudio[] = [];
  private current: { audio: HTMLAudioElement; ctx: AudioContext; url: string; settle: () => void } | null = null;
  private generation = 0;
  constructor(private readonly hooks: {
    onStart(audio: StreamAudio, analyser: AnalyserNode | null, currentMs: () => number): void;
    onEnd(audio: StreamAudio): void;
    onIdle(): void;
    onError(error: Error): void;
  }) {}

  get busy(): boolean { return !!this.current || this.queue.length > 0; }

  push(audio: StreamAudio): void {
    this.queue.push(audio);
    if (!this.current) void this.next(this.generation);
  }

  clear(): void {
    this.generation += 1;
    this.queue = [];
    this.stopCurrent();
  }

  private stopCurrent(): void {
    const playing = this.current; this.current = null;
    if (!playing) return;
    playing.settle();
    playing.audio.pause(); playing.audio.removeAttribute('src'); playing.audio.load();
    URL.revokeObjectURL(playing.url);
    if (playing.ctx.state !== 'closed') void playing.ctx.close();
  }

  private async next(generation: number): Promise<void> {
    const item = this.queue.shift();
    if (!item) { this.hooks.onIdle(); return; }
    const url = URL.createObjectURL(item.blob), audio = new Audio(url);
    const ctx = new AudioContext();
    let settle = () => {};
    const current = { audio, ctx, url, settle: () => settle() };
    this.current = current;
    try {
      await ctx.resume();
      const source = ctx.createMediaElementSource(audio), analyser = ctx.createAnalyser();
      analyser.fftSize = 512; source.connect(analyser); analyser.connect(ctx.destination);
      await new Promise<void>((resolve, reject) => {
        settle = resolve;
        if (this.current !== current) { resolve(); return; }
        audio.onplaying = () => this.hooks.onStart(item, analyser, () => heardMs(audio.currentTime, ctx.outputLatency));
        audio.onended = () => resolve();
        audio.onerror = () => reject(new Error(t('Could not play speech audio.')));
        void audio.play().catch(reject);
      });
      if (generation !== this.generation) return;
      this.hooks.onEnd(item);
    } catch (e) {
      if (generation === this.generation) this.hooks.onError(e as Error);
    } finally {
      if (this.current?.audio === audio) this.stopCurrent();
    }
    if (generation === this.generation) void this.next(generation);
  }
}
