import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router';
import { Pencil, RefreshCw, X } from 'lucide-react';
import { Button, IconButton } from '../../components';
import { getJson, responseReason } from '../../adapters/api';
import { listProjects, type Project } from '../../adapters/projects';
import { useLang } from '../../i18n';
import './personal-assistants.css';

type Assistant = { id: string; session_id: string; name: string; description: string; project_id: string; state: string; preview: string };
type Session = { id: string; name: string };

async function save(path: string, method: string, body?: unknown): Promise<Assistant> {
  const response = await fetch('/api/mobile/assistants' + path, { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) throw new Error(await responseReason(response, path));
  return response.json();
}

export function PersonalAssistants() {
  const lang = useLang();
  const tr = (en: string, es: string) => lang === 'es' ? es : en;
  const [assistants, setAssistants] = useState<Assistant[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [project, setProject] = useState('');
  const [session, setSession] = useState('');
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const data = await getJson<{ assistants: Assistant[] }>('/api/mobile/assistants', signal);
    if (!signal?.aborted) { setAssistants(data.assistants); setLoaded(true); }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    const load = () => { if (!document.hidden) void refresh(controller.signal).catch(e => { if (!controller.signal.aborted) setError(e.message); }); };
    load();
    const timer = setInterval(load, 5000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [refresh]);
  const begin = async (assistant?: Assistant) => {
    setName(assistant?.name || ''); setDescription(assistant?.description || ''); setProject(assistant?.project_id || ''); setSession(''); setEditing(assistant?.id || ''); setError('');
    try {
      const [p, s] = await Promise.all([listProjects(), getJson<{ sessions: Session[] }>('/api/mobile/sessions?limit=100')]);
      setProjects(p); setSessions(s.sessions);
    } catch (e) { setError((e as Error).message); }
  };
  const remove = async (id: string) => {
    setBusy(true); setError('');
    try { await save('/' + encodeURIComponent(id), 'DELETE'); await refresh(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const stateLabel = (s: string) => ({ running: tr('Working', 'Trabajando'), ready: tr('Ready', 'Disponible'), archived: tr('Chat archived', 'Chat archivado'), unavailable: tr('Chat unavailable', 'Chat no disponible') })[s] || tr('Status unavailable', 'Estado no disponible');
  return <section className="fs-assistants" aria-labelledby="personal-assistants-heading">
    <header className="fs-assistants__heading"><h2 id="personal-assistants-heading">{tr('Your assistants', 'Tus asistentes')}</h2><div className="fs-assistants__actions"><IconButton icon={RefreshCw} label={tr('Refresh assistants', 'Actualizar asistentes')} onClick={() => void refresh().catch(e => setError(e.message))} /><Button label={tr('New assistant', 'Nuevo asistente')} size="sm" onClick={() => void begin()} /></div></header>
    <p className="fs-prose">{tr('Give a continuing conversation a name. Open the same chat from desktop or mobile, with its project, files, tools and voice.', 'Pon nombre a una conversación que quieras continuar. Abre el mismo chat desde el ordenador o el móvil, con su proyecto, archivos, herramientas y voz.')}</p>
    {editing !== null && <form className="fs-assistants__form" onSubmit={e => {
      e.preventDefault(); setBusy(true); setError('');
      void save(editing ? '/' + encodeURIComponent(editing) : '', editing ? 'PATCH' : 'POST', { name, description, project_id: project, session_id: session })
        .then(async () => { setEditing(null); await refresh(); }).catch(e => setError(e.message)).finally(() => setBusy(false));
    }}>
      <label>{tr('Assistant name', 'Nombre del asistente')}<input className="fs-field" required maxLength={120} value={name} disabled={busy} onChange={e => setName(e.target.value)} /></label>
      <label>{tr('Usage notes', 'Notas de uso')}<textarea className="fs-field" rows={2} maxLength={1000} value={description} disabled={busy} onChange={e => setDescription(e.target.value)} /></label>
      <p className="fs-set__help">{tr('These notes describe your assistant. Its working instructions are those of its project and conversation.', 'Estas notas describen al asistente. Sus instrucciones de trabajo son las de su proyecto y conversación.')}</p>
      {!editing && <div className="fs-assistants__fields"><label>{tr('Project', 'Proyecto')}<select className="fs-field" value={project} disabled={busy || !!session} onChange={e => setProject(e.target.value)}><option value="">{tr('No project', 'Sin proyecto')}</option>{projects.filter(p => !p.archived).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <label>{tr('Conversation', 'Conversación')}<select className="fs-field" value={session} disabled={busy} onChange={e => setSession(e.target.value)}><option value="">{tr('Create a new chat', 'Crear un chat nuevo')}</option>{sessions.map(s => <option key={s.id} value={s.id}>{s.name || s.id}</option>)}</select></label></div>}
      <div className="fs-assistants__actions"><Button type="submit" label={tr('Save assistant', 'Guardar asistente')} disabled={busy || !name.trim()} loading={busy} /><Button label={tr('Cancel', 'Cancelar')} variant="ghost" disabled={busy} onClick={() => setEditing(null)} /></div>
    </form>}
    {!loaded && !error && <p role="status">{tr('Loading assistants…', 'Cargando asistentes…')}</p>}
    {loaded && !assistants.length && <p className="fs-set__help">{tr('Create an assistant or keep an existing chat here. Its history stays in Faustus.', 'Crea un asistente o guarda aquí un chat existente. Su historial permanece en Faustus.')}</p>}
    <ul className="fs-assistants__list">{assistants.map(a => <li key={a.id}>
      <div className="fs-assistants__body"><div className="fs-assistants__heading">{a.state === 'unavailable' ? <strong>{a.name}</strong> : <Link to={'/studio?s=' + encodeURIComponent(a.session_id)}>{a.name}</Link>}<span role="status" data-working={a.state === 'running'}>{stateLabel(a.state)}</span></div>
        {a.description && <p>{a.description}</p>}{a.preview && <p className="fs-assistants__preview">{a.preview}</p>}
      </div><div className="fs-assistants__actions"><IconButton icon={Pencil} label={tr('Edit assistant', 'Editar asistente')} disabled={busy || a.state === 'unavailable'} onClick={() => void begin(a)} /><IconButton icon={X} label={tr('Remove assistant; keep chat', 'Quitar asistente; conservar chat')} disabled={busy} onClick={() => void remove(a.id)} /></div>
    </li>)}</ul>
    {error && <p role="alert">{error}</p>}
  </section>;
}
