import { useEffect, useId, useState } from 'react';
import { Video } from 'lucide-react';
import { Button } from './Button';
import { walkthrough, type Recording } from '../adapters/task-walkthrough';

/** A delivery action in the existing turn summary, using the normal Prospero job. */
export function WalkthroughButton({ answer }: { answer: string }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState(() => answer.match(/https?:\/\/[^\s<>\[\])]+/)?.[0] ?? '');
  const [captions, setCaptions] = useState('Vista inicial\nContenido de la página\nResultado');
  const [recording, setRecording] = useState<Recording | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = Boolean(recording && !['done', 'failed', 'cancelled'].includes(recording.state));

  useEffect(() => {
    if (!pending || !recording?.job_id) return;
    let active = true;
    const timer = window.setTimeout(() => {
      walkthrough({ job_id: recording.job_id }).then(result => {
        if (active) { setRecording(result); setError(result.error ?? ''); }
      }).catch(reason => {
        if (active) { setError((reason as Error).message); setRecording(current => current ? { ...current } : null); }
      });
    }, 2000);
    return () => { active = false; window.clearTimeout(timer); };
  }, [pending, recording]);

  async function start() {
    setBusy(true); setError('');
    try {
      const lines = captions.split('\n').map(line => line.trim()).filter(Boolean);
      const result = await walkthrough({
        url: url.trim(), title: lines[0] ?? '',
        steps: lines.slice(1).map(caption => ({ action: 'scroll', y: 520, caption })), hold_s: 3,
      });
      setRecording(result);
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  return <div data-testid="turn-walkthrough">
    <Button icon={Video} size="sm" label="Vídeo-recorrido" aria-expanded={open} onClick={() => setOpen(!open)} />
    {open && <div className="fs-studio__edit">
      <p>Graba la web terminada con rótulos y desplazamientos. La web debe estar accesible desde Prospero; la grabación empieza sin sesión iniciada.</p>
      <label htmlFor={`${id}-url`}>Dirección de la app o web</label>
      <input id={`${id}-url`} type="url" className="fs-field" placeholder="http://127.0.0.1:…" value={url} disabled={busy || pending} onChange={event => setUrl(event.target.value)} />
      <label htmlFor={`${id}-captions`}>Rótulos, uno por vista</label>
      <textarea id={`${id}-captions`} className="fs-field" style={{ height: 'auto', paddingBlock: 'var(--fs-space-2)' }} rows={3} value={captions} disabled={busy || pending} onChange={event => setCaptions(event.target.value)} />
      <div className="fs-studio__ask-actions">
        <Button label={recording?.state === 'done' ? 'Grabar otro recorrido' : 'Grabar recorrido'} loading={busy || pending} disabled={!url.trim()} onClick={() => void start()} />
      </div>
      <p role="status">{pending ? `Grabando en Prospero${recording?.progress ? ` · ${Math.round(recording.progress * 100)} %` : ''}` : recording?.state === 'failed' ? `No se pudo grabar: ${recording.message ?? 'Revisa la dirección y vuelve a intentarlo.'}` : recording?.state === 'cancelled' ? 'Grabación cancelada' : ''}</p>
      {error && <p role="alert" className="fs-notice" data-tone="danger">{error}</p>}
      {recording?.video_url && <>
        <video controls preload="metadata" src={recording.video_url} style={{ width: '100%', maxWidth: 800 }} aria-label="Vídeo del recorrido" />
        <p><a className="fs-link" href={recording.video_url} download="recorrido.mp4">Descargar vídeo</a></p>
      </>}
    </div>}
  </div>;
}
