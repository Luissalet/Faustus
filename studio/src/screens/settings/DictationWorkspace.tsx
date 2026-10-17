import { useCallback, useEffect, useRef, useState } from 'react';
import { Copy, RefreshCw, Trash2 } from 'lucide-react';
import { Button, IconButton } from '../../components';
import { getJson, responseReason } from '../../adapters/api';
import { listProjects, type Project } from '../../adapters/projects';
import { useLang } from '../../i18n';
import './dictation-workspace.css';

type Entry = { id: string; project: string; heard: string; replacement: string };
type Dictation = { id: string; created: number; raw_text: string; text: string; status: string; target: string };

async function change(path: string, method: string, body?: unknown) {
  const r = await fetch('/api/dictation/' + path, { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) throw new Error(await responseReason(r, path));
  return r.json();
}

export function DictationWorkspace() {
  const lang = useLang();
  const tr = (en: string, es: string) => lang === 'es' ? es : en;
  const [project, setProject] = useState('');
  const [projects, setProjects] = useState<Project[]>([]);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [history, setHistory] = useState<Dictation[]>([]);
  const [heard, setHeard] = useState('');
  const [replacement, setReplacement] = useState('');
  const [editing, setEditing] = useState('');
  const [draft, setDraft] = useState('');
  const [learn, setLearn] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const generation = useRef(0);
  const refresh = useCallback(async () => {
    const revision = ++generation.current;
    const q = '?project=' + encodeURIComponent(project);
    const [v, h] = await Promise.all([getJson<{ entries: Entry[] }>('/api/dictation/vocabulary' + q), getJson<{ entries: Dictation[] }>('/api/dictation/history' + q)]);
    if (revision === generation.current) { setEntries(v.entries); setHistory(h.entries); }
  }, [project]);
  useEffect(() => { void listProjects().then(setProjects).catch(e => setMessage(String(e.message))); }, []);
  useEffect(() => { setEditing(''); setEntries([]); setHistory([]); void refresh().catch(e => setMessage(String(e.message))); return () => { ++generation.current; }; }, [refresh]);
  const act = async (work: () => Promise<unknown>, success = '') => {
    setBusy(true); setMessage('');
    try { await work(); await refresh(); setMessage(success); } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  };
  return <div className="fs-dictation">
    <h3>{tr('Vocabulary and dictation history', 'Vocabulario e historial de dictado')}</h3>
    <p className="fs-set__help">{tr('Correct a name once to reuse its spelling. Global vocabulary applies to desktop dictation; project entries apply when that project is supplied.', 'Corrige un nombre una vez para reutilizar su escritura. El vocabulario general se aplica al dictado global; las entradas de proyecto, al dictado de ese proyecto.')}</p>
    <label className="fs-dictation__scope">{tr('Vocabulary scope', 'Ámbito del vocabulario')}
      <select className="fs-field" value={project} disabled={busy} onChange={e => setProject(e.target.value)}>
        <option value="">{tr('All projects', 'Todos los proyectos')}</option>
        {projects.filter(p => !p.archived).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select>
    </label>
    <form className="fs-dictation__form" onSubmit={e => { e.preventDefault(); void act(async () => { await change('vocabulary', 'POST', { heard, replacement, project }); setHeard(''); setReplacement(''); }); }}>
      <label>{tr('Heard as', 'Se reconoce como')}<input className="fs-field" required maxLength={120} value={heard} onChange={e => setHeard(e.target.value)} /></label>
      <label>{tr('Write as', 'Escribir como')}<input className="fs-field" required maxLength={120} value={replacement} onChange={e => setReplacement(e.target.value)} /></label>
      <Button type="submit" disabled={busy || !heard.trim() || !replacement.trim()} label={tr('Save spelling', 'Guardar escritura')} />
    </form>
    <ul className="fs-dictation__list" aria-label={tr('Saved vocabulary', 'Vocabulario guardado')}>
      {entries.map(e => <li key={e.id}><span><b>{e.heard}</b> → {e.replacement}{project && !e.project ? tr(' · global', ' · general') : ''}</span><IconButton label={tr('Delete spelling', 'Eliminar escritura')} icon={Trash2} disabled={busy} onClick={() => void act(() => change('vocabulary/' + e.id, 'DELETE'))} /></li>)}
    </ul>
    <div className="fs-dictation__heading"><h4>{tr('Recent dictations', 'Dictados recientes')}</h4><IconButton label={tr('Refresh dictations', 'Actualizar dictados')} icon={RefreshCw} disabled={busy} onClick={() => void act(async () => {})} /></div>
    <p className="fs-set__help">{tr('Text is saved before pasting. Copy it here if the target app could not receive it. Audio is not stored in this history.', 'El texto se guarda antes de pegar. Cópialo desde aquí si la aplicación no pudo recibirlo. Este historial no guarda el audio.')}</p>
    {!history.length && <p>{tr('No dictations in this scope yet.', 'Todavía no hay dictados en este ámbito.')}</p>}
    <ol className="fs-dictation__list">
      {history.map(h => <li key={h.id}><article>
        <div className="fs-dictation__heading"><time dateTime={new Date(h.created * 1000).toISOString()}>{new Date(h.created * 1000).toLocaleString(lang)}</time><span>{h.status === 'pasted' ? tr('Pasted', 'Pegado') : tr('Ready to recover', 'Listo para recuperar')}</span></div>
        {h.target && <small>{h.target}</small>}
        {editing === h.id ? <form onSubmit={e => { e.preventDefault(); void act(async () => { await change('history/' + h.id, 'PATCH', { text: draft, learn }); setEditing(''); }); }}>
          <label>{tr('Corrected text', 'Texto corregido')}<textarea className="fs-field" rows={4} value={draft} required maxLength={16000} onChange={e => setDraft(e.target.value)} /></label>
          <label><input type="checkbox" checked={learn} onChange={e => setLearn(e.target.checked)} /> {tr('Learn name spellings', 'Aprender la escritura de nombres')}</label>
          <div className="fs-dictation__actions"><Button type="submit" disabled={busy} label={tr('Save correction', 'Guardar corrección')} /><Button label={tr('Cancel', 'Cancelar')} variant="ghost" onClick={() => setEditing('')} /></div>
        </form> : <p className="fs-dictation__text">{h.text}</p>}
        <details><summary>{tr('Original transcript', 'Transcripción original')}</summary><p className="fs-dictation__text">{h.raw_text}</p></details>
        <div className="fs-dictation__actions"><Button icon={Copy} label={tr('Copy text', 'Copiar texto')} variant="ghost" size="sm" onClick={() => void act(() => navigator.clipboard.writeText(h.text), tr('Copied', 'Copiado'))} /><Button label={tr('Correct', 'Corregir')} variant="ghost" size="sm" disabled={busy} onClick={() => { setEditing(h.id); setDraft(h.text); }} /><IconButton label={tr('Delete dictation', 'Eliminar dictado')} icon={Trash2} disabled={busy} onClick={() => void act(() => change('history/' + h.id, 'DELETE'))} /></div>
      </article></li>)}
    </ol>
    <p role="status" aria-live="polite">{message || (busy ? tr('Saving…', 'Guardando…') : '')}</p>
  </div>;
}
