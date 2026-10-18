import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { Mic, Square, VolumeX, X, Send, Settings2 } from 'lucide-react';
import { locale, t } from '../i18n';
import type { Turn } from '../screens/studio/model';
import { capabilities, capture, openMic, playSpeech, watchForSpeech, type Capture, type CaptureOptions, type OpenMic, type SpeechCapabilities } from './audio';
import { SentenceBuffer, isEcho, isHallucination, isStopPhrase, speechLanguage, stripWakeWord, dictationEditTimeout, type VoicePhase } from './engine';
import { VoiceOrb } from './VoiceOrb';
import { VoiceSubtitle } from './VoiceSubtitle';
import type { Playback } from './visemes';
import { postTimeline, timelineDetail, timelineLine, type VoiceMarks } from './timeline';
import { PcmTap, StreamPlayer, VoiceStream, streamCapture, voiceStreamCapabilities, type StreamAudio, type VoiceStreamCapabilities } from './stream';
import './voice.css';

const labels: Record<VoicePhase, string> = {
  idle: 'Ready when you are', starting: 'Opening microphone…', listening: 'Listening to you',
  transcribing: 'Turning your voice into text…', review: 'Is this what you meant?',
  thinking: 'Faustus is working', speaking: 'Faustus is speaking', approval: 'Your approval is needed', error: 'Let’s reconnect',
};
interface Props { busy: boolean; turn?: Turn; sessionName: string; sessionId?: string | null; projectId?: string; onSend(text: string): void; onStop(): void; onClose(): void }

/* A conversation, not a dictation form: by default the panel listens as
   soon as it opens, sends what it heard when you stop talking, reads the
   answer and listens again. The three switches stay in "Voice options" for
   whoever wants to review each transcript; what you choose is kept. */
const PREFS_KEY = 'faustus_voice_prefs';
type SilenceMs = 600 | 900 | 1500;
/* `serverStream`: audio goes to the server over one WebSocket (with pre-roll)
   and replies come back already spoken sentence by sentence (./stream.ts). Off
   = the previous path: record, POST, and synthesize each sentence on demand. */
interface VoicePrefs { continuous: boolean; autoSend: boolean; readAloud: boolean | null; silenceMs: SilenceMs; wakeWord: boolean; natural: boolean; serverStream: boolean }
function readPrefs(): VoicePrefs {
  try {
    const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') as Partial<VoicePrefs>;
    const silenceMs: SilenceMs = raw.silenceMs === 600 || raw.silenceMs === 1500 ? raw.silenceMs : 900;
    return { continuous: raw.continuous ?? true, autoSend: raw.autoSend ?? true, readAloud: raw.readAloud ?? null, silenceMs, wakeWord: raw.wakeWord ?? false, natural: raw.natural ?? true, serverStream: raw.serverStream ?? true };
  } catch { return { continuous: true, autoSend: true, readAloud: null, silenceMs: 900, wakeWord: false, natural: true, serverStream: true }; }
}
// A short pool of the last things Faustus said, so a barge-in (or an
// utterance heard right after TTS stops) that is actually the mic
// re-hearing Faustus itself can be told apart from a real interruption.
const ECHO_GUARD_MS = 1500;
function writePrefs(patch: Partial<VoicePrefs>): void {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify({ ...readPrefs(), ...patch })); } catch { /* private mode */ }
}

export default function VoicePanel(props: Props) {
  const [phase, setPhase] = useState<VoicePhase>(props.busy ? 'thinking' : 'idle');
  const [configs, setConfigs] = useState<{ stt: SpeechCapabilities; tts: SpeechCapabilities } | null>(null);
  const [error, setError] = useState('');
  const [transcript, setTranscript] = useState('');
  const [original, setOriginal] = useState('');
  const [previousDraft, setPreviousDraft] = useState<string | null>(null);
  const revising = useRef(false);
  const [revisionInstruction, setRevisionInstruction] = useState('');
  const prefs = useRef(readPrefs());
  const [continuous, setContinuousState] = useState(prefs.current.continuous);
  const [natural, setNatural] = useState(prefs.current.natural);
  const [autoSend, setAutoSendState] = useState(prefs.current.autoSend);
  const [readAloud, setReadAloud] = useState(prefs.current.readAloud ?? true);
  /* Only the person's own toggles are remembered: the panel also switches
     `continuous` off by itself when a turn errors or asks for approval,
     and that must not become the default for tomorrow. */
  const chooseContinuous = (on: boolean) => { setContinuousState(on); writePrefs({ continuous: on }); };
  const chooseAutoSend = (on: boolean) => { setAutoSendState(on); writePrefs({ autoSend: on }); };
  const setContinuous = setContinuousState;
  const [silenceMs, setSilenceMsState] = useState<SilenceMs>(prefs.current.silenceMs);
  const chooseSilenceMs = (ms: SilenceMs) => { setSilenceMsState(ms); writePrefs({ silenceMs: ms }); };
  const [wakeWord, setWakeWordState] = useState(prefs.current.wakeWord);
  const chooseWakeWord = (on: boolean) => { setWakeWordState(on); writePrefs({ wakeWord: on }); };
  const [interrupted, setInterrupted] = useState(false);
  const [latency, setLatency] = useState('');
  const [deviceId, setDeviceId] = useState('');
  const [language, setLanguage] = useState('auto');
  const heardLanguage = useRef(locale());
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [analyser, setAnalyser] = useState<AnalyserNode | null>(null);
  // What is being said right now and where the audio is, for the orb's mouth and the subtitle (null when nothing plays).
  const [playback, setPlayback] = useState<Playback | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [shortened, setShortened] = useState(false);
  const recording = useRef<Capture | null>(null);
  const input = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const latest = useRef({ ...props, continuous, autoSend, readAloud }); latest.current = { ...props, continuous, autoSend, readAloud };
  const reply = useRef({ id: props.turn?.id, controller: new AbortController(), buffer: new SentenceBuffer(), queue: [] as string[], draining: false, muted: false, speechEndAt: 0, heardMs: 0, firstAudioLogged: false, marks: null as VoiceMarks | null, posted: false });
  const engaged = useRef(false);
  const autoStart = useRef(false);
  const nextListen = useRef<() => void>(() => {});
  const phaseRef = useRef(phase); phaseRef.current = phase;
  // The hot microphone shared by every hands-free turn and by the barge-in
  // watcher, so neither has to wait on a fresh permission/device round trip.
  const mic = useRef<OpenMic | null>(null);
  // Last few seconds of Faustus's own speech, for the echo guard.
  const lastSpoken = useRef({ text: '', at: 0 });
  // end-of-speech timestamp for the in-flight utterance, used for the
  // "heard in / first reply audio in" latency readout.
  const timing = useRef({ endOfSpeech: 0, heardMs: 0 });
  const pendingTiming = useRef<{ speechEndAt: number; heardMs: number; marks: VoiceMarks | null } | null>(null);
  // Wall-clock marks of the utterance being captured (see ./timeline.ts); handed to the reply when it is sent.
  const utteranceMarks = useRef<VoiceMarks>({});
  const [timeline, setTimeline] = useState<{ line: string; detail: string } | null>(null);
  // Streaming voice session (./stream.ts): the WebSocket, the PCM tap on the
  // hot mic (its pre-roll keeps the first syllable of a barge-in) and the
  // player of sentences the server already synthesized.
  const [serverStream, setServerStream] = useState(prefs.current.serverStream);
  const [streamCaps, setStreamCaps] = useState<VoiceStreamCapabilities | null>(null);
  const [streamReady, setStreamReady] = useState(false);
  // False while the voice session is still opening: the first hands-free turn waits for it (briefly) so it streams too.
  const [streamSettled, setStreamSettled] = useState(false);
  const stream = useRef<VoiceStream | null>(null);
  const tap = useRef<PcmTap | null>(null);
  const player = useRef<StreamPlayer | null>(null);
  // Server-side runs whose speech is still on its way, and the ones silenced by the person.
  const streamSpeech = useRef({ pending: new Set<string>(), muted: new Set<string>(), played: new Set<string>(), lastRun: '' });
  const streamOut = useRef(false);
  streamOut.current = streamReady && serverStream && readAloud && !!streamCaps?.speech_output && configs?.tts.execution !== 'browser';
  const streamIn = streamReady && serverStream && !!streamCaps?.available && configs?.stt.execution !== 'browser';
  /** Show the line and store the turn; partial turns (no audio) are stored once, when the reply ends. */
  const finishTimeline = useCallback((run: typeof reply.current) => {
    if (!run.marks || run.posted || !run.id) return;
    run.posted = true;
    const line = timelineLine(run.marks);
    if (line) setTimeline({ line, detail: timelineDetail(run.marks) });
    postTimeline(latest.current.sessionId || '', String(run.id), run.marks);
  }, []);

  const ensureMic = useCallback(async (signal: AbortSignal): Promise<OpenMic | null> => {
    if (mic.current) return mic.current;
    try {
      const opened = await openMic(deviceId);
      if (signal.aborted || !alive.current) { opened.close(); return null; }
      mic.current = opened; return opened;
    } catch { return null; }
  }, [deviceId]);

  const silence = useCallback(() => {
    reply.current.muted = true; reply.current.queue = []; reply.current.controller.abort(); reply.current.draining = false;
    // Streamed speech: stop what plays, drop what is queued, and tell the
    // server to stop speaking this reply. The agent's work is not cancelled.
    const speech = streamSpeech.current;
    if (player.current?.busy || speech.pending.size) {
      speech.pending.forEach(runId => speech.muted.add(runId));
      if (speech.lastRun) speech.muted.add(speech.lastRun);
      speech.pending.clear();
      player.current?.clear();
      stream.current?.interrupt('user');
    }
    setAnalyser(null); setPlayback(null);
  }, []);
  const pause = useCallback(() => {
    engaged.current = false; input.current?.abort(); input.current = null;
    recording.current = null; silence(); setPhase(revising.current ? 'review' : 'idle'); revising.current = false; setRevisionInstruction(''); setContinuous(false);
    tap.current?.close(); tap.current = null;
    mic.current?.close(); mic.current = null;
  }, [silence]);
  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    Promise.all([capabilities('stt', controller.signal), capabilities('tts', controller.signal), voiceStreamCapabilities(controller.signal)])
      .then(([stt, tts, streaming]) => {
        if (controller.signal.aborted) return;
        setStreamCaps(streaming);
        setConfigs({ stt, tts });
        const speakable = tts.configured && tts.dependency_installed;
        setReadAloud(speakable && (prefs.current.readAloud ?? true));
        // Hands-free from the first second: the person opened voice mode
        // to talk, so start listening unless a task is already running.
        if (stt.configured && stt.dependency_installed && !latest.current.busy && prefs.current.continuous) autoStart.current = true;
      })
      .catch(e => { if (!controller.signal.aborted) { setError(e.message); setPhase('error'); } });
    const hidden = () => { if (document.hidden) pause(); };
    document.addEventListener('visibilitychange', hidden);
    return () => {
      alive.current = false; controller.abort(); input.current?.abort(); reply.current.controller.abort();
      document.removeEventListener('visibilitychange', hidden);
      tap.current?.close(); tap.current = null;
      mic.current?.close(); mic.current = null;
    };
  }, [pause]);
  useEffect(() => {
    setElapsed(0);
    if (phase === 'idle' || phase === 'review' || phase === 'error') return;
    const start = Date.now(); const timer = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [phase]);

  // The streaming voice session of this chat. It only listens for this chat's
  // runs and speaks them; Studio still sends every message itself.
  useEffect(() => {
    const sid = props.sessionId;
    if (!sid || !serverStream || !streamCaps?.available || !configs || configs.stt.execution === 'browser') { setStreamReady(false); setStreamSettled(Boolean(configs)); return; }
    const controller = new AbortController();
    setStreamSettled(false);
    const settleAnyway = window.setTimeout(() => setStreamSettled(true), 2500);
    const speech = streamSpeech.current;
    speech.pending.clear(); speech.muted.clear(); speech.played.clear(); speech.lastRun = '';
    let opened: VoiceStream | null = null;
    const offs: Array<() => void> = [];
    const finishSpeech = () => {
      if (!alive.current || speech.pending.size || player.current?.busy) return;
      setAnalyser(null); setPlayback(null);
      if (phaseRef.current !== 'speaking' && phaseRef.current !== 'thinking') return;
      const turn = latest.current.turn;
      setPhase(turn?.ask ? 'approval' : latest.current.busy ? 'thinking' : 'idle');
      if (!latest.current.busy && !turn?.ask && latest.current.continuous && engaged.current && !document.hidden) nextListen.current();
    };
    const playing = new StreamPlayer({
      onStart: (audio, node, currentMs) => {
        if (!alive.current) return;
        setAnalyser(node); setPhase('speaking');
        setPlayback({ text: audio.text, marks: null, currentMs });
        lastSpoken.current = { text: audio.text, at: performance.now() };
        const run = reply.current;
        if (run.marks && !run.marks.first_audio) { run.marks.first_audio = Date.now(); finishTimeline(run); }
        if (!run.firstAudioLogged && run.speechEndAt) {
          run.firstAudioLogged = true;
          setLatency(t('Heard in {heard}s · first reply audio in {first}s', {
            heard: (run.heardMs / 1000).toFixed(1), first: ((performance.now() - run.speechEndAt) / 1000).toFixed(1) }));
        }
        if (!speech.played.has(audio.runId)) { speech.played.add(audio.runId); stream.current?.played(audio.runId); }
      },
      onEnd: () => { if (alive.current) setPlayback(null); },
      onIdle: finishSpeech,
      onError: e => { if (alive.current) setError(e.message); },
    });
    player.current = playing;
    VoiceStream.open(sid, { language, languageHint: heardLanguage.current.split('-')[0], readAloud: readAloud && !!streamCaps.speech_output, partials: true }, controller.signal)
      .then(opening => {
        if (controller.signal.aborted || !alive.current) { opening.close(); return; }
        opened = opening; stream.current = opening;
        offs.push(opening.onEvent(event => {
          const runId = String(event.run_id || '');
          if (event.type === 'turn' && runId) {
            speech.lastRun = runId;
            if (event.state === 'running' && !speech.muted.has(runId)) speech.pending.add(runId);
          } else if ((event.type === 'speech_end' || event.type === 'interrupted') && runId) {
            speech.pending.delete(runId);
            finishSpeech();
          } else if (event.type === 'error' && event.stage === 'tts' && alive.current) {
            setError(String(event.message || ''));
          }
        }));
        offs.push(opening.onAudio((audio: StreamAudio) => {
          if (!alive.current || !streamOut.current || speech.muted.has(audio.runId)) return;
          const run = reply.current;
          if (run.marks && !run.marks.tts_chunk) { run.marks.tts_chunk = Date.now(); run.marks.tts_server_ms = audio.synthMs; }
          playing.push(audio);
        }));
        setStreamReady(true); setStreamSettled(true);
      })
      .catch(err => { console.debug('[voice] streaming session unavailable; per-sentence speech is used', err); setStreamReady(false); setStreamSettled(true); });
    return () => {
      clearTimeout(settleAnyway);
      controller.abort(); offs.forEach(off => off()); playing.clear();
      if (player.current === playing) player.current = null;
      opened?.close(); if (stream.current === opened) stream.current = null;
      setStreamReady(false);
    };
    // Reopened only for another chat or when switched on/off; options travel over the open stream.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.sessionId, serverStream, streamCaps, configs]);
  useEffect(() => { stream.current?.options({ read_aloud: readAloud && !!streamCaps?.speech_output, language }); }, [readAloud, language, streamCaps, streamReady]);

  /** `reviewed`: the person read and sent the transcript by hand, so the wait is theirs, not the system's: no timeline. */
  const commit = useCallback((text: string, reviewed = false) => {
    if (!text.trim() || latest.current.busy) return;
    const marks = reviewed || !utteranceMarks.current.speech_end ? null : { ...utteranceMarks.current, request_sent: Date.now() };
    pendingTiming.current = { speechEndAt: timing.current.endOfSpeech, heardMs: timing.current.heardMs, marks };
    setTimeline(null);
    engaged.current = true; setTranscript(''); setPhase('thinking'); setError('');
    latest.current.onSend(text.trim());
  }, []);

  const listen = useCallback(async () => {
    if (!configs || recording.current || phaseRef.current === 'starting' || phaseRef.current === 'transcribing') return;
    silence(); input.current?.abort();
    const controller = new AbortController(); input.current = controller;
    setError(''); setTranscript(''); setOriginal(''); setPreviousDraft(null); setPhase('starting'); setInterrupted(false);
    const openedMic = await ensureMic(controller.signal);
    // Streaming input: PCM over the voice session, pre-roll included.
    if (openedMic && streamIn && stream.current?.connected && !tap.current) tap.current = new PcmTap(openedMic);
    const viaStream = openedMic && streamIn && stream.current?.connected && tap.current
      ? (options: CaptureOptions) => streamCapture(stream.current!, tap.current!, options) : undefined;
    try {
      const cap = await capture(configs.stt, {
        signal: controller.signal, deviceId, autoStop: true, lang: language, languageHint: heardLanguage.current, raw: viaStream,
        mic: openedMic ?? undefined, silenceMs, natural, project: props.projectId,
        onPartial: text => { if (!controller.signal.aborted) setTranscript(text); },
        onTranscribing: () => { if (!controller.signal.aborted) { timing.current.endOfSpeech = performance.now(); utteranceMarks.current = { speech_end: Date.now() }; setPhase('transcribing'); setAnalyser(null); } },
      });
      if (controller.signal.aborted) { cap.cancel(); return; }
      recording.current = cap; setAnalyser(cap.analyser); setPhase('listening');
      void navigator.mediaDevices?.enumerateDevices().then(ds => { if (!controller.signal.aborted) setDevices(ds.filter(d => d.kind === 'audioinput')); }).catch(() => {});
      const text = await cap.done;
      if (controller.signal.aborted || !alive.current) return;
      recording.current = null; setAnalyser(null);
      timing.current.heardMs = timing.current.endOfSpeech ? performance.now() - timing.current.endOfSpeech : 0;
      if (utteranceMarks.current.speech_end) {
        utteranceMarks.current.transcript_ready = Date.now();
        if (typeof cap.sttServerMs === 'number') utteranceMarks.current.stt_server_ms = cap.sttServerMs;
      }
      const rawText = cap.rawText || text;
      if (!text) { setPhase('idle'); setError(t('I did not hear anything. Try again closer to the microphone.')); setContinuous(false); return; }
      // Discard silently and keep listening: Whisper's own silence
      // hallucinations, the mic re-hearing Faustus (echo), and anything
      // that is only a "stop talking" instruction never reach the model.
      if (isHallucination(rawText)) { console.debug('[voice] discarded (hallucination):', text); void listen(); return; }
      if (isEcho(rawText, lastSpoken.current.text, ECHO_GUARD_MS, performance.now() - lastSpoken.current.at)) {
        console.debug('[voice] discarded (echo of Faustus’ own speech):', text); void listen(); return;
      }
      if (isStopPhrase(rawText, configs.stt.stop_phrases)) { console.debug('[voice] stop phrase, not sent:', text); silence(); void listen(); return; }
      let toSend = text;
      if (wakeWord) {
        const stripped = stripWakeWord(rawText);
        if (!stripped.matched) { console.debug('[voice] no wake word, discarded:', text); void listen(); return; }
        toSend = stripWakeWord(text).text || stripped.text || text;
      }
      setOriginal(wakeWord ? stripWakeWord(rawText).text || rawText : rawText);
      heardLanguage.current = cap.language || (language === 'auto' ? locale() : language);
      setTranscript(toSend);
      if (latest.current.autoSend && !latest.current.busy) commit(toSend);
      else setPhase('review');
    } catch (e) {
      if (!controller.signal.aborted && alive.current) {
        recording.current = null; setAnalyser(null); setPhase('error'); setContinuous(false);
        setError((e as Error).name === 'NotAllowedError' ? t('Microphone permission was denied. Allow it in your browser and retry.') : (e as Error).message);
      }
    }
  }, [configs, deviceId, language, silence, commit, ensureMic, silenceMs, wakeWord, natural, streamIn]);
  const revise = useCallback(async () => {
    if (!configs || !transcript.trim() || recording.current || revising.current) return;
    const draft = transcript;
    silence(); input.current?.abort();
    const controller = new AbortController(); input.current = controller;
    revising.current = true; setRevisionInstruction(''); setError(''); setPhase('starting');
    try {
      const openedMic = await ensureMic(controller.signal);
      const cap = await capture(configs.stt, {
        signal: controller.signal, deviceId, autoStop: true, lang: language, languageHint: heardLanguage.current,
        mic: openedMic ?? undefined, silenceMs, natural: false,
        onPartial: text => { if (!controller.signal.aborted) setRevisionInstruction(text); },
        onTranscribing: () => { if (!controller.signal.aborted) { setPhase('transcribing'); setAnalyser(null); } },
      });
      if (controller.signal.aborted) { cap.cancel(); return; }
      recording.current = cap; setAnalyser(cap.analyser); setPhase('listening');
      const instruction = await cap.done;
      controller.signal.throwIfAborted();
      recording.current = null; setAnalyser(null); setRevisionInstruction(instruction);
      if (!instruction || isStopPhrase(instruction, configs.stt.stop_phrases) || isHallucination(instruction)) return;
      setPhase('transcribing');
      const response = await fetch('/api/stt/polish', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: instruction, mode: 'revise', draft }),
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(dictationEditTimeout(instruction, draft))]),
      });
      if (!response.ok) throw new Error('revision failed');
      const result = await response.json() as { text?: string; status?: string };
      controller.signal.throwIfAborted();
      if (alive.current && ['edited', 'unchanged'].includes(result.status || '') && typeof result.text === 'string' && result.text.trim()) {
        if (result.text.trim() !== draft) setPreviousDraft(draft);
        setTranscript(result.text.trim());
      } else throw new Error('revision unavailable');
    } catch {
      if (!controller.signal.aborted && alive.current) setError(t('Could not apply the correction. Your draft is unchanged.'));
    } finally {
      if (alive.current && input.current === controller) {
        recording.current = null; setAnalyser(null); revising.current = false; setRevisionInstruction(''); setPhase('review');
      }
    }
  }, [configs, transcript, silence, ensureMic, deviceId, language, silenceMs]);
  nextListen.current = () => void listen();
  // `listen` closes over `configs`, so the first listen waits for the
  // render that has them.
  useEffect(() => {
    if (!configs || !autoStart.current || !streamSettled) return;
    autoStart.current = false;
    engaged.current = true;
    void listen();
  }, [configs, listen, streamSettled]);
  // Keep the pre-roll ring filling from the moment the hot mic and the stream are both there,
  // so even the first barge-in keeps its first syllables.
  useEffect(() => {
    if (streamIn && mic.current && !tap.current) tap.current = new PcmTap(mic.current);
  }, [streamIn, phase]);

  const drain = useCallback(async () => {
    const run = reply.current;
    if (run.draining || run.muted || !configs) return;
    run.draining = true;
    try {
      while (run.queue.length && !run.controller.signal.aborted) {
        const text = run.queue.shift()!;
        setPhase('thinking');
        lastSpoken.current = { text, at: performance.now() };
        const responseLanguage = speechLanguage(latest.current.turn?.text || text, heardLanguage.current);
        await playSpeech(text, { ...configs.tts, language: responseLanguage }, run.controller.signal, (node, playing) => {
          if (alive.current && !run.controller.signal.aborted) {
            setAnalyser(node);
            if (playing) {
              setPhase('speaking');
              if (run.marks && !run.marks.first_audio) { run.marks.first_audio = Date.now(); finishTimeline(run); }
              if (!run.firstAudioLogged && run.speechEndAt) {
                run.firstAudioLogged = true;
                const heardSec = (run.heardMs / 1000).toFixed(1);
                const firstSec = ((performance.now() - run.speechEndAt) / 1000).toFixed(1);
                const label = t('Heard in {heard}s · first reply audio in {first}s', { heard: heardSec, first: firstSec });
                setLatency(label);
                console.debug('[voice]', label);
              }
            }
          }
        }, info => {
          if (run.marks && !run.marks.tts_chunk) {
            run.marks.tts_chunk = Date.now();
            if (info.serverMs != null) run.marks.tts_server_ms = info.serverMs;
          }
        }, pb => { if (alive.current && (pb === null || !run.controller.signal.aborted)) setPlayback(pb); });
      }
    } catch (e) {
      if (!run.controller.signal.aborted && alive.current) { setError((e as Error).message); setReadAloud(false); setContinuous(false); run.queue = []; }
    } finally {
      run.draining = false;
      if (alive.current && reply.current === run && !run.controller.signal.aborted) {
        setAnalyser(null);
        setPhase(latest.current.turn?.ask ? 'approval' : latest.current.busy ? 'thinking' : 'idle');
        if (!latest.current.busy && !latest.current.turn?.ask && latest.current.continuous && engaged.current && !document.hidden) nextListen.current();
      }
    }
  }, [configs, finishTimeline]);

  useEffect(() => {
    const turn = props.turn;
    if (!engaged.current) {
      // Opening voice during an existing task observes its status without
      // reading old messages aloud or leaving a finished task as "working".
      if (['idle', 'thinking', 'approval'].includes(phaseRef.current)) {
        setPhase(turn?.ask ? 'approval' : props.busy ? 'thinking' : 'idle');
      }
      return;
    }
    if (!turn) return;
    if (reply.current.id !== turn.id) {
      reply.current.controller.abort();
      const timingForTurn = pendingTiming.current; pendingTiming.current = null;
      reply.current = {
        id: turn.id, controller: new AbortController(), buffer: new SentenceBuffer(), queue: [], draining: false, muted: false,
        speechEndAt: timingForTurn?.speechEndAt ?? 0, heardMs: timingForTurn?.heardMs ?? 0, firstAudioLogged: false,
        marks: timingForTurn?.marks ?? null, posted: false,
      };
      setShortened(false);
    }
    const run = reply.current;
    if (run.marks && !run.marks.first_token && turn.text.trim()) run.marks.first_token = Date.now();
    if (turn.ask) { silence(); setPhase('approval'); setContinuous(false); return; }
    if (turn.error) { silence(); setPhase('error'); setError(turn.error); setContinuous(false); return; }
    if (['starting', 'listening', 'transcribing', 'review'].includes(phaseRef.current)) return;
    // With the streaming session the server speaks this run itself (already
    // synthesized ahead); the panel only plays it and waits for its end.
    const streamed = streamOut.current;
    if (readAloud && !run.muted && !streamed) {
      run.queue.push(...run.buffer.take(turn.text, !props.busy));
      if (run.buffer.truncated) setShortened(true);
      if (run.queue.length) void drain();
    }
    const streamSpeaking = streamed && (streamSpeech.current.pending.size > 0 || !!player.current?.busy);
    if (!run.draining && !run.queue.length && !streamSpeaking) {
      if (!props.busy) finishTimeline(run);   // the reply ended without (more) audio: store what was measured
      setPhase(props.busy ? 'thinking' : 'idle');
      if (!props.busy && continuous && engaged.current && !document.hidden) nextListen.current();
    }
  }, [props.turn, props.busy, drain, readAloud, continuous, silence, finishTimeline]);

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.altKey && e.code === 'KeyV' && !e.repeat) { e.preventDefault(); if (recording.current) recording.current.stop(); else nextListen.current(); }
      if (e.key === 'Escape') pause();
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [pause]);

  // Barge-in: while Faustus is speaking, keep watching the (already hot)
  // microphone; sustained speech cuts the reply off and starts recording
  // that utterance immediately, on the same open stream.
  useEffect(() => {
    if (phase !== 'speaking' || !continuous || !mic.current) return;
    const controller = new AbortController();
    watchForSpeech(mic.current.analyser, controller.signal, () => {
      if (controller.signal.aborted) return;
      silence();
      setInterrupted(true);
      window.setTimeout(() => setInterrupted(false), 1500);
      void listen();
    });
    return () => controller.abort();
  }, [phase, continuous, silence, listen]);

  const captureActive = ['starting', 'listening', 'transcribing'].includes(phase);
  const providerLabel = (cap: SpeechCapabilities) => cap.execution === 'local' ? t('On this server') : cap.execution === 'browser' ? t('Browser') : cap.execution === 'endpoint' ? t('Configured endpoint · may be remote') : t('Disabled');
  const tool = props.turn?.steps.slice().reverse().find(s => s.state === 'running');
  return <section className="fs-voice" aria-label={t('Voice conversation')} data-phase={phase} data-testid="voice-panel">
    <div className="fs-voice__visual"><VoiceOrb analyser={analyser} phase={phase} playback={playback} /><span className="fs-voice__micstate">{t(phase === 'listening' || (phase === 'speaking' && continuous) ? 'Microphone active' : 'Microphone off')}</span></div>
    <div className="fs-voice__body">
      <div className="fs-voice__heading"><h2>{t('Talk to Faustus')}</h2><button type="button" className="fs-voice__icon" onClick={props.onClose} aria-label={t('Close voice mode')}><X size={18} /></button></div>
      <p className="fs-voice__context">{props.sessionName} · {t('Same chat, same tools')}</p>
      {configs && <p className="fs-voice__hint">{t('Input')}: {providerLabel(configs.stt)} · {t('Output')}: {providerLabel(configs.tts)}</p>}
      <label className="fs-voice__language">{t('Conversation language')}<select value={configs?.stt.provider === 'parakeet' ? 'auto' : language} disabled={configs?.stt.provider === 'parakeet' || captureActive || props.busy || phase === 'speaking'} onChange={e => setLanguage(e.target.value)}>
        <option value="auto">{t('Automatic · English / Spanish')}</option><option value="es">Español</option><option value="en">English</option>
      </select></label>
      {configs?.stt.provider === 'parakeet' && <p className="fs-voice__hint">{t('Parakeet detects the language automatically. Very short utterances can be mistaken for another language.')}</p>}
      {configs?.stt.execution === 'browser' && language === 'auto' && <p className="fs-voice__hint">{t('Browser recognition needs a fixed language. Choose English or Spanish; automatic detection uses local recognition.')}</p>}
      <div className="fs-voice__status" role="status"><strong>{interrupted ? t('Go ahead') : t(labels[phase])}</strong>{elapsed > 0 && <span>{elapsed}s</span>}</div>
      <VoiceSubtitle playback={playback} />
      {wakeWord && !revising.current && !interrupted && (phase === 'listening' || phase === 'starting') && <p className="fs-voice__hint">{t('Waiting for “Faustus”')}</p>}
      {revising.current && <p className="fs-voice__hint">{revisionInstruction || t('Say what to change in your draft. It will stay here for review.')}</p>}
      {timeline && (
        <div className="fs-voice__timeline-wrap">
          <p className="fs-voice__hint fs-voice__latency fs-voice__timeline" data-testid="voice-timeline" aria-describedby="voice-timeline-detail" title={timeline.detail} tabIndex={0}>{timeline.line}</p>
          <pre id="voice-timeline-detail" role="tooltip" className="fs-voice__timeline-detail" data-testid="voice-timeline-detail">{timeline.detail}</pre>
        </div>
      )}
      {latency && !timeline && <p className="fs-voice__hint fs-voice__latency">{latency}</p>}
      {phase === 'thinking' && <p className="fs-voice__hint">{tool ? `${t('Using tool')}: ${tool.label || tool.tool}` : t('The model may need time to load. You can keep using the app.')}</p>}
      {phase === 'approval' && <p>{t('Review the approval in the chat. Spoken answers cannot approve sensitive actions.')}</p>}
      {error && <p className="fs-voice__error" role="alert">{error}</p>}
      {configs && (!configs.stt.configured || !configs.stt.dependency_installed) && <p className="fs-voice__hint">{t('Enable a speech recognition provider in Settings → Voice.')} <Link to="/settings?s=voice">{t('Open speech settings')}</Link></p>}
      {(transcript || phase === 'review') && <label className="fs-voice__transcript">{t('Your message')}<textarea value={transcript} onChange={e => setTranscript(e.target.value)} readOnly={phase !== 'review'} rows={2} /></label>}
      <div className="fs-voice__actions">
        {phase === 'review' && <button type="button" disabled={!configs?.stt.configured || !configs.stt.dependency_installed || !transcript.trim()} onClick={() => void revise()}><Mic size={16} />{t('Correct by voice')}</button>}
        {phase === 'review' && previousDraft !== null && previousDraft !== transcript && <button type="button" onClick={() => { setTranscript(previousDraft); setPreviousDraft(null); }}>{t('Undo voice correction')}</button>}
        {phase === 'review' && original && original !== transcript && <button type="button" onClick={() => setTranscript(original)}>{t('Use original transcription')}</button>}
        {phase === 'review' ? <button type="button" className="fs-voice__primary" disabled={props.busy || !transcript.trim()} onClick={() => commit(transcript, true)}><Send size={16} />{t(props.busy ? 'Waiting for the current task' : 'Send message')}</button>
        : <button type="button" className="fs-voice__primary" disabled={!configs || !configs.stt.configured || !configs.stt.dependency_installed || phase === 'starting' || phase === 'transcribing'} onClick={() => recording.current ? recording.current.stop() : void listen()}><Mic size={16} />{t(phase === 'listening' ? 'Finish speaking' : phase === 'speaking' ? 'Interrupt and speak' : 'Start speaking')}</button>}
        {phase === 'speaking' && <button type="button" onClick={() => { silence(); setPhase(props.busy ? 'thinking' : 'idle'); }}><VolumeX size={16} />{t('Silence voice')}</button>}
        <button type="button" onClick={pause}><Square size={14} />{t('Mute microphone')}</button>
        {props.busy && <button type="button" onClick={() => { pause(); props.onStop(); }}>{t('Cancel task')}</button>}
      </div>
      <details className="fs-voice__settings"><summary><Settings2 size={14} />{t('Voice options & privacy')}</summary>
        <label><input type="checkbox" checked={readAloud} disabled={!configs?.tts.configured || !configs.tts.dependency_installed} onChange={e => { setReadAloud(e.target.checked); writePrefs({ readAloud: e.target.checked }); if (!e.target.checked) { silence(); if (phaseRef.current === 'speaking') setPhase(props.busy ? 'thinking' : 'idle'); } }} />{t('Read responses aloud')}</label>
        <label><input type="checkbox" checked={continuous} onChange={e => chooseContinuous(e.target.checked)} />{t('Listen again after each response')}</label>
        {streamCaps?.available && <label><input type="checkbox" checked={serverStream} disabled={captureActive} onChange={e => { setServerStream(e.target.checked); writePrefs({ serverStream: e.target.checked }); }} />{t('Streaming voice · the server transcribes as you speak and voices the answer while it is written')}</label>}
        <label><input type="checkbox" checked={natural} disabled={captureActive} onChange={e => { setNatural(e.target.checked); writePrefs({ natural: e.target.checked }); }} />{t('Natural dictation · clean fillers and self-corrections')}</label>
        <label><input type="checkbox" checked={autoSend} onChange={e => chooseAutoSend(e.target.checked)} />{t('Send without reviewing transcription')}</label>
        <label><input type="checkbox" checked={wakeWord} onChange={e => chooseWakeWord(e.target.checked)} />{t('Only answer when I say Faustus first')}</label>
        <fieldset className="fs-voice__silence"><legend>{t('Pause that ends your turn')}</legend>
          <label><input type="radio" name="fs-voice-silence" checked={silenceMs === 600} onChange={() => chooseSilenceMs(600)} />{t('Short')}</label>
          <label><input type="radio" name="fs-voice-silence" checked={silenceMs === 900} onChange={() => chooseSilenceMs(900)} />{t('Normal')}</label>
          <label><input type="radio" name="fs-voice-silence" checked={silenceMs === 1500} onChange={() => chooseSilenceMs(1500)} />{t('Long')}</label>
        </fieldset>
        {devices.length > 0 && <label>{t('Microphone')}<select value={deviceId} disabled={captureActive} onChange={e => setDeviceId(e.target.value)}><option value="">{t('System default')}</option>{devices.map((d, i) => <option key={d.deviceId || i} value={d.deviceId}>{d.label || `${t('Microphone')} ${i + 1}`}</option>)}</select></label>}
        {configs?.stt.execution === 'browser' && <p>{t('Browser recognition may send audio to its speech service. Choose a local provider for local transcription.')}</p>}
        <p>{t('Audio capture is temporary. Voice replies skip the disk cache. Final messages follow this chat’s history settings. Endpoint retention depends on its provider.')}</p>
        <p>{t('Listening pauses while Faustus speaks. Use Interrupt and speak to take your turn. Changing chat or hiding this tab mutes voice; the task continues.')}</p>
        <Link to="/settings?s=voice">{t('Open speech settings')}</Link>
      </details>
      {shortened && <p className="fs-voice__hint">{t('Voice excerpt finished. The complete answer, including code and tables, is in the chat.')}</p>}
      <p className="fs-voice__hint">{t('Alt + V to talk or finish · Escape to mute. Closing voice does not cancel the task.')}</p>
    </div>
  </section>;
}
