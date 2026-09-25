import { Download, FileText, Headphones, RefreshCw } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from '../../components';
import { podcastStatus, startPodcast, type PodcastState } from '../../adapters/research';
import { loadPiperStatus, type PiperVoiceInfo } from '../../adapters/piper';
import { t } from '../../i18n';
import '../research.css';

/**
 * The podcast of one finished research report (src/research_podcast.py):
 * start it, follow the job, then play / download it and read the script.
 * `autostart` starts a job on mount when the report has no podcast yet —
 * used where the panel opens because the user pressed "Podcast".
 */

const POLL_MS = 2000;

function phaseText(s: PodcastState): string {
  switch (s.phase) {
    case 'condensing':
      return t('Condensing the report ({done}/{total})', { done: s.chunksDone, total: s.chunksTotal });
    case 'script':
      return t('Writing the script…');
    case 'synthesizing':
      return t('Voicing line {done} of {total}', { done: s.linesDone, total: s.linesTotal });
    case 'mixing':
      return t('Joining the audio…');
    case 'encoding':
      return t('Encoding MP3…');
    case 'saving':
      return t('Saving…');
    default:
      return t('Starting…');
  }
}

function minutes(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function PodcastPanel({ researchId, autostart = false, say }: { researchId: string; autostart?: boolean; say?: (m: string, tone?: 'ok' | 'warn') => void }) {
  const [state, setState] = useState<PodcastState | null>(null);
  const [loadError, setLoadError] = useState('');
  const [busy, setBusy] = useState(false);
  const [showScript, setShowScript] = useState(false);
  const autostarted = useRef(false);
  const [voices, setVoices] = useState<PiperVoiceInfo[]>([]);
  const [voiceA, setVoiceA] = useState('');
  const [voiceB, setVoiceB] = useState('');
  const [length, setLength] = useState(0);
  useEffect(() => {
    let alive = true;
    loadPiperStatus()
      .then((st) => { if (alive) setVoices(st.installed_voices || []); })
      .catch(() => { /* the picker just stays on Auto */ });
    return () => { alive = false; };
  }, []);
  // The parent's toast callback may be a new function every render; reading
  // it through a ref keeps it out of the effects' dependencies.
  const sayRef = useRef(say);
  useEffect(() => {
    sayRef.current = say;
  }, [say]);

  const start = useCallback(async () => {
    setBusy(true);
    try {
      setState(await startPodcast(researchId, { voiceA, voiceB, minutes: length || undefined }));
    } catch (err) {
      const message = (err as Error).message || t('The podcast could not be started.');
      setState((prev) => ({ ...(prev ?? emptyState()), status: 'failed', error: message }));
      sayRef.current?.(message, 'warn');
    } finally {
      setBusy(false);
    }
  }, [researchId, voiceA, voiceB, length]);

  useEffect(() => {
    const ac = new AbortController();
    podcastStatus(researchId, ac.signal)
      .then((s) => {
        setState(s);
        if (autostart && !autostarted.current && s.status === 'none') {
          autostarted.current = true;
          void start();
        }
      })
      .catch((err: Error) => {
        if (!ac.signal.aborted) setLoadError(err.message || t('Could not read the podcast state.'));
      });
    return () => ac.abort();
  }, [researchId, autostart, start]);

  useEffect(() => {
    if (state?.status !== 'running') return;
    const ac = new AbortController();
    const timer = window.setTimeout(() => {
      podcastStatus(researchId, ac.signal)
        .then((s) => {
          setState(s);
          if (s.status === 'done') sayRef.current?.(t('Podcast ready.'));
        })
        .catch(() => {
          /* next render keeps polling from the last state */
        });
    }, POLL_MS);
    return () => {
      window.clearTimeout(timer);
      ac.abort();
    };
  }, [state, researchId]);

  if (loadError) return <p className="fs-notice" data-tone="warning">{loadError}</p>;
  if (!state) return <p className="fs-rs__meta">{t('Loading…')}</p>;

  const warnings = state.warnings.length > 0 && (
    <ul className="fs-rs__podcast-warnings">
      {state.warnings.map((w, i) => (
        <li key={i} className="fs-notice" data-tone="warning">{w}</li>
      ))}
    </ul>
  );

  const voiceSelect = (value: string, set: (v: string) => void, label: string, testId: string) => (
    <label className="fs-rs__podcast-opt">
      <span>{label}</span>
      <select value={value} onChange={(e) => set(e.target.value)} data-testid={testId}>
        <option value="">{t('Automatic')}</option>
        {voices.map((v) => (
          <option key={v.name} value={v.name}>{`${v.name} (${v.language})`}</option>
        ))}
      </select>
    </label>
  );
  const options = state.status !== 'running' && (
    <div className="fs-rs__podcast-row fs-rs__podcast-opts">
      {voiceSelect(voiceA, setVoiceA, t('Voice A'), 'research-podcast-voice-a')}
      {voiceSelect(voiceB, setVoiceB, t('Voice B'), 'research-podcast-voice-b')}
      <label className="fs-rs__podcast-opt">
        <span>{t('Length')}</span>
        <select value={length} onChange={(e) => setLength(Number(e.target.value))} data-testid="research-podcast-length">
          <option value={0}>{t('Default')}</option>
          {[3, 6, 10, 15].map((m) => (
            <option key={m} value={m}>{t('{n} min', { n: m })}</option>
          ))}
        </select>
      </label>
    </div>
  );

  return (
    <section className="fs-rs__podcast" data-status={state.status} data-testid="research-podcast" aria-label={t('Podcast')}>
      {options}
      {state.status === 'none' && (
        <div className="fs-rs__podcast-row">
          <Button variant="secondary" size="sm" icon={Headphones} label={t('Make a podcast')} loading={busy} onClick={() => void start()} testId="research-podcast-start" />
          <span className="fs-rs__meta">{t('A two-voice audio version of this report, voiced by local Piper voices.')}</span>
        </div>
      )}

      {state.status === 'running' && (
        <div className="fs-rs__podcast-progress" role="status">
          <span className="fs-rs__meta">{phaseText(state)}</span>
          {state.phase === 'synthesizing' && state.linesTotal > 0 ? (
            <progress max={state.linesTotal} value={state.linesDone} />
          ) : (
            <progress />
          )}
        </div>
      )}

      {state.status === 'failed' && (
        <div className="fs-rs__podcast-row">
          <p className="fs-notice" data-tone="warning">{state.error || t('The podcast failed.')}</p>
          <Button variant="secondary" size="sm" icon={RefreshCw} label={t('Try again')} loading={busy} onClick={() => void start()} />
        </div>
      )}

      {state.status === 'done' && state.audioUrl && (
        <>
          <audio className="fs-rs__podcast-audio" controls preload="metadata" src={state.audioUrl} data-testid="research-podcast-audio" />
          <div className="fs-rs__podcast-row">
            {state.downloadUrl && (
              <a className="fs-btn" data-variant="ghost" data-size="sm" href={state.downloadUrl} download>
                <Download size={16} aria-hidden="true" />
                <span>{t('Download audio')}</span>
              </a>
            )}
            {state.script.length > 0 && (
              <Button variant="ghost" size="sm" icon={FileText} label={showScript ? t('Hide transcript') : t('Show transcript')} onClick={() => setShowScript((v) => !v)} />
            )}
            {state.transcriptUrl && (
              <a className="fs-btn" data-variant="ghost" data-size="sm" href={state.transcriptUrl} download>
                <span>{t('Transcript (.md)')}</span>
              </a>
            )}
            <span className="fs-rs__meta">
              {[state.durationS ? minutes(state.durationS) : '', state.voices ? `${state.voices.A} · ${state.voices.B}` : ''].filter(Boolean).join(' · ')}
            </span>
            <span className="fs-spacer" />
            <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Make it again')} loading={busy} onClick={() => void start()} />
          </div>
        </>
      )}

      {warnings}

      {showScript && state.script.length > 0 && (
        <ol className="fs-rs__podcast-script">
          {state.script.map((line, i) => (
            <li key={i} data-speaker={line.speaker}>
              <strong>{line.speaker}</strong> {line.text}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function emptyState(): PodcastState {
  return {
    status: 'none', phase: '', linesDone: 0, linesTotal: 0, chunksDone: 0, chunksTotal: 0,
    script: [], warnings: [], error: '', audioUrl: null, downloadUrl: null, transcriptUrl: null,
    durationS: 0, voices: null,
  };
}
