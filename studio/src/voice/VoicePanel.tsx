import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { Mic, Square, VolumeX, X, Send, Settings2 } from 'lucide-react';
import { locale, t } from '../i18n';
import type { Turn } from '../screens/studio/model';
import { capabilities, capture, playSpeech, type Capture, type SpeechCapabilities } from './audio';
import { SentenceBuffer, speechLanguage, type VoicePhase } from './engine';
import { VoiceOrb } from './VoiceOrb';
import './voice.css';

const labels: Record<VoicePhase, string> = {
  idle: 'Ready when you are', starting: 'Opening microphone…', listening: 'Listening to you',
  transcribing: 'Turning your voice into text…', review: 'Is this what you meant?',
  thinking: 'Faustus is working', speaking: 'Faustus is speaking', approval: 'Your approval is needed', error: 'Let’s reconnect',
};
interface Props { busy: boolean; turn?: Turn; sessionName: string; onSend(text: string): void; onStop(): void; onClose(): void }

export default function VoicePanel(props: Props) {
  const [phase, setPhase] = useState<VoicePhase>(props.busy ? 'thinking' : 'idle');
  const [configs, setConfigs] = useState<{ stt: SpeechCapabilities; tts: SpeechCapabilities } | null>(null);
  const [error, setError] = useState('');
  const [transcript, setTranscript] = useState('');
  const [continuous, setContinuous] = useState(false);
  const [autoSend, setAutoSend] = useState(false);
  const [readAloud, setReadAloud] = useState(true);
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
  const reply = useRef({ id: props.turn?.id, controller: new AbortController(), buffer: new SentenceBuffer(), queue: [] as string[], draining: false, muted: false });
  const engaged = useRef(false);
  const nextListen = useRef<() => void>(() => {});
  const phaseRef = useRef(phase); phaseRef.current = phase;

  const silence = useCallback(() => {
    reply.current.muted = true; reply.current.queue = []; reply.current.controller.abort(); reply.current.draining = false;
    setAnalyser(null);
  }, []);
  const pause = useCallback(() => {
    engaged.current = false; input.current?.abort(); input.current = null;
    recording.current = null; silence(); setPhase('idle'); setContinuous(false);
  }, [silence]);
  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    Promise.all([capabilities('stt', controller.signal), capabilities('tts', controller.signal)])
      .then(([stt, tts]) => { if (!controller.signal.aborted) { setConfigs({ stt, tts }); setReadAloud(tts.configured && tts.dependency_installed); } })
      .catch(e => { if (!controller.signal.aborted) { setError(e.message); setPhase('error'); } });
    const hidden = () => { if (document.hidden) pause(); };
    document.addEventListener('visibilitychange', hidden);
    return () => {
      alive.current = false; controller.abort(); input.current?.abort(); reply.current.controller.abort();
      document.removeEventListener('visibilitychange', hidden);
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
    engaged.current = true; setTranscript(''); setPhase('thinking'); setError('');
    latest.current.onSend(text.trim());
  }, []);

  const listen = useCallback(async () => {
    if (!configs || recording.current || phaseRef.current === 'starting' || phaseRef.current === 'transcribing') return;
    silence(); input.current?.abort();
    const controller = new AbortController(); input.current = controller;
    setError(''); setTranscript(''); setPhase('starting');
    try {
      const cap = await capture(configs.stt, {
        signal: controller.signal, deviceId, autoStop: true, lang: language,
        onPartial: text => { if (!controller.signal.aborted) setTranscript(text); },
        onTranscribing: () => { if (!controller.signal.aborted) { setPhase('transcribing'); setAnalyser(null); } },
      });
      if (controller.signal.aborted) { cap.cancel(); return; }
      recording.current = cap; setAnalyser(cap.analyser); setPhase('listening');
      void navigator.mediaDevices?.enumerateDevices().then(ds => { if (!controller.signal.aborted) setDevices(ds.filter(d => d.kind === 'audioinput')); }).catch(() => {});
      const text = await cap.done;
      if (controller.signal.aborted || !alive.current) return;
      recording.current = null; setAnalyser(null);
      heardLanguage.current = cap.language || (language === 'auto' ? locale() : language);
      if (!text) { setPhase('idle'); setError(t('I did not hear anything. Try again closer to the microphone.')); setContinuous(false); return; }
      setTranscript(text);
      if (latest.current.autoSend && !latest.current.busy) commit(text);
      else setPhase('review');
    } catch (e) {
      if (!controller.signal.aborted && alive.current) {
        recording.current = null; setAnalyser(null); setPhase('error'); setContinuous(false);
        setError((e as Error).name === 'NotAllowedError' ? t('Microphone permission was denied. Allow it in your browser and retry.') : (e as Error).message);
      }
    }
  }, [configs, deviceId, language, silence, commit]);
  nextListen.current = () => void listen();

  const drain = useCallback(async () => {
    const run = reply.current;
    if (run.draining || run.muted || !configs) return;
    run.draining = true;
    try {
      while (run.queue.length && !run.controller.signal.aborted) {
        const text = run.queue.shift()!;
        setPhase('thinking');
        const responseLanguage = speechLanguage(latest.current.turn?.text || text, heardLanguage.current);
        await playSpeech(text, { ...configs.tts, language: responseLanguage }, run.controller.signal, (node, playing) => {
          if (alive.current && !run.controller.signal.aborted) { setAnalyser(node); if (playing) setPhase('speaking'); }
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
      reply.current = { id: turn.id, controller: new AbortController(), buffer: new SentenceBuffer(), queue: [], draining: false, muted: false };
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

  const captureActive = ['starting', 'listening', 'transcribing'].includes(phase);
  const providerLabel = (cap: SpeechCapabilities) => cap.execution === 'local' ? t('On this server') : cap.execution === 'browser' ? t('Browser') : cap.execution === 'endpoint' ? t('Configured endpoint · may be remote') : t('Disabled');
  const tool = props.turn?.steps.slice().reverse().find(s => s.state === 'running');
  return <section className="fs-voice" aria-label={t('Voice conversation')} data-phase={phase} data-testid="voice-panel">
    <div className="fs-voice__visual"><VoiceOrb analyser={analyser} phase={phase} /><span className="fs-voice__micstate">{t(phase === 'listening' ? 'Microphone active' : 'Microphone off')}</span></div>
    <div className="fs-voice__body">
      <div className="fs-voice__heading"><h2>{t('Talk to Faustus')}</h2><button type="button" className="fs-voice__icon" onClick={props.onClose} aria-label={t('Close voice mode')}><X size={18} /></button></div>
      <p className="fs-voice__context">{props.sessionName} · {t('Same chat, same tools')}</p>
      {configs && <p className="fs-voice__hint">{t('Input')}: {providerLabel(configs.stt)} · {t('Output')}: {providerLabel(configs.tts)}</p>}
      <label className="fs-voice__language">{t('Conversation language')}<select value={language} disabled={captureActive || props.busy || phase === 'speaking'} onChange={e => setLanguage(e.target.value)}>
        <option value="auto">{t('Automatic · English / Spanish')}</option><option value="es">Español</option><option value="en">English</option>
      </select></label>
      {configs?.stt.execution === 'browser' && language === 'auto' && <p className="fs-voice__hint">{t('Browser recognition needs a fixed language. Choose English or Spanish; automatic detection uses local Whisper.')}</p>}
      <div className="fs-voice__status" role="status"><strong>{t(labels[phase])}</strong>{elapsed > 0 && <span>{elapsed}s</span>}</div>
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
        <label><input type="checkbox" checked={readAloud} disabled={!configs?.tts.configured || !configs.tts.dependency_installed} onChange={e => { setReadAloud(e.target.checked); if (!e.target.checked) { silence(); if (phaseRef.current === 'speaking') setPhase(props.busy ? 'thinking' : 'idle'); } }} />{t('Read responses aloud')}</label>
        <label><input type="checkbox" checked={continuous} onChange={e => setContinuous(e.target.checked)} />{t('Listen again after each response')}</label>
        <label><input type="checkbox" checked={autoSend} onChange={e => setAutoSend(e.target.checked)} />{t('Send without reviewing transcription')}</label>
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
