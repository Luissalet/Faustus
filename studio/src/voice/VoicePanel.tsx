import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { Mic, Square, VolumeX, X, Send, Settings2 } from 'lucide-react';
import { locale, t } from '../i18n';
import type { Turn } from '../screens/studio/model';
import { capabilities, capture, openMic, playSpeech, watchForSpeech, type Capture, type OpenMic, type SpeechCapabilities } from './audio';
import { SentenceBuffer, isEcho, isHallucination, isStopPhrase, speechLanguage, stripWakeWord, type VoicePhase } from './engine';
import { VoiceOrb } from './VoiceOrb';
import './voice.css';

const labels: Record<VoicePhase, string> = {
  idle: 'Ready when you are', starting: 'Opening microphone…', listening: 'Listening to you',
  transcribing: 'Turning your voice into text…', review: 'Is this what you meant?',
  thinking: 'Faustus is working', speaking: 'Faustus is speaking', approval: 'Your approval is needed', error: 'Let’s reconnect',
};
interface Props { busy: boolean; turn?: Turn; sessionName: string; onSend(text: string): void; onStop(): void; onClose(): void }

/* A conversation, not a dictation form: by default the panel listens as
   soon as it opens, sends what it heard when you stop talking, reads the
   answer and listens again. The three switches stay in "Voice options" for
   whoever wants to review each transcript; what you choose is kept. */
const PREFS_KEY = 'faustus_voice_prefs';
type SilenceMs = 600 | 900 | 1500;
interface VoicePrefs { continuous: boolean; autoSend: boolean; readAloud: boolean | null; silenceMs: SilenceMs; wakeWord: boolean }
function readPrefs(): VoicePrefs {
  try {
    const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') as Partial<VoicePrefs>;
    const silenceMs: SilenceMs = raw.silenceMs === 600 || raw.silenceMs === 1500 ? raw.silenceMs : 900;
    return { continuous: raw.continuous ?? true, autoSend: raw.autoSend ?? true, readAloud: raw.readAloud ?? null, silenceMs, wakeWord: raw.wakeWord ?? false };
  } catch { return { continuous: true, autoSend: true, readAloud: null, silenceMs: 900, wakeWord: false }; }
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
  const prefs = useRef(readPrefs());
  const [continuous, setContinuousState] = useState(prefs.current.continuous);
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
  const [elapsed, setElapsed] = useState(0);
  const [shortened, setShortened] = useState(false);
  const recording = useRef<Capture | null>(null);
  const input = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const latest = useRef({ ...props, continuous, autoSend, readAloud }); latest.current = { ...props, continuous, autoSend, readAloud };
  const reply = useRef({ id: props.turn?.id, controller: new AbortController(), buffer: new SentenceBuffer(), queue: [] as string[], draining: false, muted: false, speechEndAt: 0, heardMs: 0, firstAudioLogged: false });
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
  const pendingTiming = useRef<{ speechEndAt: number; heardMs: number } | null>(null);

  const ensureMic = useCallback(async (): Promise<OpenMic | null> => {
    if (mic.current) return mic.current;
    try { mic.current = await openMic(deviceId); return mic.current; } catch { return null; }
  }, [deviceId]);

  const silence = useCallback(() => {
    reply.current.muted = true; reply.current.queue = []; reply.current.controller.abort(); reply.current.draining = false;
    setAnalyser(null);
  }, []);
  const pause = useCallback(() => {
    engaged.current = false; input.current?.abort(); input.current = null;
    recording.current = null; silence(); setPhase('idle'); setContinuous(false);
    mic.current?.close(); mic.current = null;
  }, [silence]);
  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    Promise.all([capabilities('stt', controller.signal), capabilities('tts', controller.signal)])
      .then(([stt, tts]) => {
        if (controller.signal.aborted) return;
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
      mic.current?.close(); mic.current = null;
    };
  }, [pause]);
  useEffect(() => {
    setElapsed(0);
    if (phase === 'idle' || phase === 'review' || phase === 'error') return;
    const start = Date.now(); const timer = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [phase]);

  const commit = useCallback((text: string) => {
    if (!text.trim() || latest.current.busy) return;
    pendingTiming.current = { speechEndAt: timing.current.endOfSpeech, heardMs: timing.current.heardMs };
    engaged.current = true; setTranscript(''); setPhase('thinking'); setError('');
    latest.current.onSend(text.trim());
  }, []);

  const listen = useCallback(async () => {
    if (!configs || recording.current || phaseRef.current === 'starting' || phaseRef.current === 'transcribing') return;
    silence(); input.current?.abort();
    const controller = new AbortController(); input.current = controller;
    setError(''); setTranscript(''); setPhase('starting'); setInterrupted(false);
    const openedMic = await ensureMic();
    try {
      const cap = await capture(configs.stt, {
        signal: controller.signal, deviceId, autoStop: true, lang: language,
        mic: openedMic ?? undefined, silenceMs,
        onPartial: text => { if (!controller.signal.aborted) setTranscript(text); },
        onTranscribing: () => { if (!controller.signal.aborted) { timing.current.endOfSpeech = performance.now(); setPhase('transcribing'); setAnalyser(null); } },
      });
      if (controller.signal.aborted) { cap.cancel(); return; }
      recording.current = cap; setAnalyser(cap.analyser); setPhase('listening');
      void navigator.mediaDevices?.enumerateDevices().then(ds => { if (!controller.signal.aborted) setDevices(ds.filter(d => d.kind === 'audioinput')); }).catch(() => {});
      const text = await cap.done;
      if (controller.signal.aborted || !alive.current) return;
      recording.current = null; setAnalyser(null);
      heardLanguage.current = cap.language || (language === 'auto' ? locale() : language);
      timing.current.heardMs = timing.current.endOfSpeech ? performance.now() - timing.current.endOfSpeech : 0;
      if (!text) { setPhase('idle'); setError(t('I did not hear anything. Try again closer to the microphone.')); setContinuous(false); return; }
      // Discard silently and keep listening: Whisper's own silence
      // hallucinations, the mic re-hearing Faustus (echo), and anything
      // that is only a "stop talking" instruction never reach the model.
      if (isHallucination(text)) { console.debug('[voice] discarded (hallucination):', text); void listen(); return; }
      if (isEcho(text, lastSpoken.current.text, ECHO_GUARD_MS, performance.now() - lastSpoken.current.at)) {
        console.debug('[voice] discarded (echo of Faustus’ own speech):', text); void listen(); return;
      }
      if (isStopPhrase(text, configs.stt.stop_phrases)) { console.debug('[voice] stop phrase, not sent:', text); silence(); void listen(); return; }
      let toSend = text;
      if (wakeWord) {
        const stripped = stripWakeWord(text);
        if (!stripped.matched) { console.debug('[voice] no wake word, discarded:', text); void listen(); return; }
        toSend = stripped.text || text;
      }
      setTranscript(toSend);
      if (latest.current.autoSend && !latest.current.busy) commit(toSend);
      else setPhase('review');
    } catch (e) {
      if (!controller.signal.aborted && alive.current) {
        recording.current = null; setAnalyser(null); setPhase('error'); setContinuous(false);
        setError((e as Error).name === 'NotAllowedError' ? t('Microphone permission was denied. Allow it in your browser and retry.') : (e as Error).message);
      }
    }
  }, [configs, deviceId, language, silence, commit, ensureMic, silenceMs, wakeWord]);
  nextListen.current = () => void listen();
  // `listen` closes over `configs`, so the first listen waits for the
  // render that has them.
  useEffect(() => {
    if (!configs || !autoStart.current) return;
    autoStart.current = false;
    engaged.current = true;
    void listen();
  }, [configs, listen]);

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
        });
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
  }, [configs]);

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
      };
      setShortened(false);
    }
    const run = reply.current;
    if (turn.ask) { silence(); setPhase('approval'); setContinuous(false); return; }
    if (turn.error) { silence(); setPhase('error'); setError(turn.error); setContinuous(false); return; }
    if (['starting', 'listening', 'transcribing', 'review'].includes(phaseRef.current)) return;
    if (readAloud && !run.muted) {
      run.queue.push(...run.buffer.take(turn.text, !props.busy));
      if (run.buffer.truncated) setShortened(true);
      if (run.queue.length) void drain();
    }
    if (!run.draining && !run.queue.length) {
      setPhase(props.busy ? 'thinking' : 'idle');
      if (!props.busy && continuous && engaged.current && !document.hidden) nextListen.current();
    }
  }, [props.turn, props.busy, drain, readAloud, continuous, silence]);

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
    <div className="fs-voice__visual"><VoiceOrb analyser={analyser} phase={phase} /><span className="fs-voice__micstate">{t(phase === 'listening' || (phase === 'speaking' && continuous) ? 'Microphone active' : 'Microphone off')}</span></div>
    <div className="fs-voice__body">
      <div className="fs-voice__heading"><h2>{t('Talk to Faustus')}</h2><button type="button" className="fs-voice__icon" onClick={props.onClose} aria-label={t('Close voice mode')}><X size={18} /></button></div>
      <p className="fs-voice__context">{props.sessionName} · {t('Same chat, same tools')}</p>
      {configs && <p className="fs-voice__hint">{t('Input')}: {providerLabel(configs.stt)} · {t('Output')}: {providerLabel(configs.tts)}</p>}
      <label className="fs-voice__language">{t('Conversation language')}<select value={language} disabled={captureActive || props.busy || phase === 'speaking'} onChange={e => setLanguage(e.target.value)}>
        <option value="auto">{t('Automatic · English / Spanish')}</option><option value="es">Español</option><option value="en">English</option>
      </select></label>
      {configs?.stt.execution === 'browser' && language === 'auto' && <p className="fs-voice__hint">{t('Browser recognition needs a fixed language. Choose English or Spanish; automatic detection uses local Whisper.')}</p>}
      <div className="fs-voice__status" role="status"><strong>{interrupted ? t('Go ahead') : t(labels[phase])}</strong>{elapsed > 0 && <span>{elapsed}s</span>}</div>
      {wakeWord && !interrupted && (phase === 'listening' || phase === 'starting') && <p className="fs-voice__hint">{t('Waiting for “Faustus”')}</p>}
      {latency && <p className="fs-voice__hint fs-voice__latency">{latency}</p>}
      {phase === 'thinking' && <p className="fs-voice__hint">{tool ? `${t('Using tool')}: ${tool.label || tool.tool}` : t('The model may need time to load. You can keep using the app.')}</p>}
      {phase === 'approval' && <p>{t('Review the approval in the chat. Spoken answers cannot approve sensitive actions.')}</p>}
      {error && <p className="fs-voice__error" role="alert">{error}</p>}
      {configs && (!configs.stt.configured || !configs.stt.dependency_installed) && <p className="fs-voice__hint">{t('Enable a speech recognition provider in Settings → Voice.')} <Link to="/settings?s=voice">{t('Open speech settings')}</Link></p>}
      {(transcript || phase === 'review') && <label className="fs-voice__transcript">{t('Your message')}<textarea value={transcript} onChange={e => setTranscript(e.target.value)} readOnly={phase !== 'review'} rows={2} /></label>}
      <div className="fs-voice__actions">
        {phase === 'review' ? <button type="button" className="fs-voice__primary" disabled={props.busy || !transcript.trim()} onClick={() => commit(transcript)}><Send size={16} />{t(props.busy ? 'Waiting for the current task' : 'Send message')}</button>
        : <button type="button" className="fs-voice__primary" disabled={!configs || !configs.stt.configured || !configs.stt.dependency_installed || phase === 'starting' || phase === 'transcribing'} onClick={() => recording.current ? recording.current.stop() : void listen()}><Mic size={16} />{t(phase === 'listening' ? 'Finish speaking' : phase === 'speaking' ? 'Interrupt and speak' : 'Start speaking')}</button>}
        {phase === 'speaking' && <button type="button" onClick={() => { silence(); setPhase(props.busy ? 'thinking' : 'idle'); }}><VolumeX size={16} />{t('Silence voice')}</button>}
        <button type="button" onClick={pause}><Square size={14} />{t('Mute microphone')}</button>
        {props.busy && <button type="button" onClick={() => { pause(); props.onStop(); }}>{t('Cancel task')}</button>}
      </div>
      <details className="fs-voice__settings"><summary><Settings2 size={14} />{t('Voice options & privacy')}</summary>
        <label><input type="checkbox" checked={readAloud} disabled={!configs?.tts.configured || !configs.tts.dependency_installed} onChange={e => { setReadAloud(e.target.checked); writePrefs({ readAloud: e.target.checked }); if (!e.target.checked) { silence(); if (phaseRef.current === 'speaking') setPhase(props.busy ? 'thinking' : 'idle'); } }} />{t('Read responses aloud')}</label>
        <label><input type="checkbox" checked={continuous} onChange={e => chooseContinuous(e.target.checked)} />{t('Listen again after each response')}</label>
        <label><input type="checkbox" checked={autoSend} onChange={e => chooseAutoSend(e.target.checked)} />{t('Send without reviewing transcription')}</label>
        <label><input type="checkbox" checked={wakeWord} onChange={e => chooseWakeWord(e.target.checked)} />{t('Only answer when I say Faustus first')}</label>
        <fieldset className="fs-voice__silence"><legend>{t('Pause that ends your turn')}</legend>
          <label><input type="radio" name="fs-voice-silence" checked={silenceMs === 600} onChange={() => chooseSilenceMs(600)} />{t('Short')}</label>
          <label><input type="radio" name="fs-voice-silence" checked={silenceMs === 900} onChange={() => chooseSilenceMs(900)} />{t('Normal')}</label>
          <label><input type="radio" name="fs-voice-silence" checked={silenceMs === 1500} onChange={() => chooseSilenceMs(1500)} />{t('Long')}</label>
        </fieldset>
        {devices.length > 0 && <label>{t('Microphone')}<select value={deviceId} disabled={captureActive} onChange={e => setDeviceId(e.target.value)}><option value="">{t('System default')}</option>{devices.map((d, i) => <option key={d.deviceId || i} value={d.deviceId}>{d.label || `${t('Microphone')} ${i + 1}`}</option>)}</select></label>}
        {configs?.stt.execution === 'browser' && <p>{t('Browser recognition may send audio to its speech service. Choose Whisper for local transcription.')}</p>}
        <p>{t('Audio capture is temporary. Voice replies skip the disk cache. Final messages follow this chat’s history settings. Endpoint retention depends on its provider.')}</p>
        <p>{t('Listening pauses while Faustus speaks. Use Interrupt and speak to take your turn. Changing chat or hiding this tab mutes voice; the task continues.')}</p>
        <Link to="/settings?s=voice">{t('Open speech settings')}</Link>
      </details>
      {shortened && <p className="fs-voice__hint">{t('Voice excerpt finished. The complete answer, including code and tables, is in the chat.')}</p>}
      <p className="fs-voice__hint">{t('Alt + V to talk or finish · Escape to mute. Closing voice does not cancel the task.')}</p>
    </div>
  </section>;
}
