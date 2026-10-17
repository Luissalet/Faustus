import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router';
import { Pencil, Play, RefreshCw, X } from 'lucide-react';
import { Button, IconButton } from '../../components';
import { getJson, responseReason } from '../../adapters/api';
import { listProjects, type Project } from '../../adapters/projects';
import { useLang } from '../../i18n';
import './personal-assistants.css';

type Run = { id: string; status: string; started_at: string; result?: string; error?: string };
type Proactivity = { enabled: boolean; schedule: string; time: string; day: number; timezone: string; cron: string; prompt: string; notifications: boolean; quiet: boolean; next_run?: string; last_run?: string; runs?: Run[] };
const defaults: Proactivity = { enabled: false, schedule: 'daily', time: '09:00', day: 0, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/Madrid', cron: '0 9 * * *', prompt: '', notifications: true, quiet: true };
type Assistant = { id: string; session_id: string; name: string; description: string; instructions?: string; skills?: string[]; proactivity?: Proactivity; project_id: string; state: string; preview: string };
type Skill = { name: string; description: string };
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
  const [instructions, setInstructions] = useState('');
  const [skills, setSkills] = useState<string[]>([]);
  const [catalog, setCatalog] = useState<Skill[]>([]);
  const [skillQuery, setSkillQuery] = useState('');
  const [proactivity, setProactivity] = useState<Proactivity>({ ...defaults });
  const [project, setProject] = useState('');
  const [session, setSession] = useState('');
  const [busy, setBusy] = useState(false);
  const showError = (e: Error) => {
    const translations: Record<string, string> = {
      'Choose a valid IANA timezone.': 'Elige una zona horaria válida, por ejemplo Europe/Madrid.',
      'Describe what the assistant should do proactively.': 'Describe qué debe hacer el asistente de forma proactiva.',
      'Use a five-field cron expression.': 'Usa una expresión cron de cinco campos.',
      'This schedule has no next occurrence.': 'Este horario no tiene una próxima ejecución. Elige otro.',
      'An assistant needs a name.': 'El asistente necesita un nombre.',
      'The task scheduler is unavailable': 'El programador no está disponible. Comprueba que Faustus está en marcha.',
    };
    setError(lang === 'es' ? translations[e.message] || e.message : e.message);
  };
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
    setInstructions(assistant?.instructions || ''); setSkills(assistant?.skills || []); setProactivity({ ...defaults, ...assistant?.proactivity }); setSkillQuery('');
    try {
      const [p, s, k] = await Promise.all([listProjects(), getJson<{ sessions: Session[] }>('/api/mobile/sessions?limit=100'), getJson<{ skills: Skill[] }>('/api/mobile/assistants/skills')]);
      setProjects(p); setSessions(s.sessions); setCatalog(k.skills);
    } catch (e) { setError((e as Error).message); }
  };
  const changeProactivity = (patch: Partial<Proactivity>) => setProactivity(p => ({ ...p, ...patch }));
  const date = (value?: string) => value ? new Date(value).toLocaleString(lang === 'es' ? 'es-ES' : 'en-GB') : '—';
  const run = async (id: string) => {
    setBusy(true); setError('');
    try { await save('/' + encodeURIComponent(id) + '/run', 'POST'); await refresh(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const remove = async (id: string) => {
    setBusy(true); setError('');
    try { await save('/' + encodeURIComponent(id), 'DELETE'); await refresh(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const stateLabel = (s: string) => ({ running: tr('Working', 'Trabajando'), ready: tr('Ready', 'Disponible'), archived: tr('Chat archived', 'Chat archivado'), unavailable: tr('Chat unavailable', 'Chat no disponible') })[s] || tr('Status unavailable', 'Estado no disponible');
  return <section className="fs-assistants" aria-labelledby="personal-assistants-heading">
    <header className="fs-assistants__heading"><h2 id="personal-assistants-heading">{tr('Your assistants', 'Tus asistentes')}</h2><div className="fs-assistants__actions"><IconButton icon={RefreshCw} label={tr('Refresh assistants', 'Actualizar asistentes')} onClick={() => void refresh().catch(e => setError(e.message))} /><Button label={tr('New assistant', 'Nuevo asistente')} size="sm" onClick={() => void begin()} /></div></header>
    <p className="fs-prose">{tr('Set each assistant’s instructions, skills and proactive mission. Work in the same continuing chat from desktop or mobile, with its project, files, tools and voice.', 'Configura las instrucciones, skills y misión proactiva de cada asistente. Trabaja en el mismo chat desde el ordenador o el móvil, con su proyecto, archivos, herramientas y voz.')}</p>
    {editing !== null && <form className="fs-assistants__form" onSubmit={e => {
      e.preventDefault(); setBusy(true); setError('');
      const { enabled, schedule, time, day, timezone, cron, prompt, notifications, quiet } = proactivity;
      void save(editing ? '/' + encodeURIComponent(editing) : '', editing ? 'PATCH' : 'POST', { name, description, instructions, skills, proactivity: { enabled, schedule, time, day, timezone, cron, prompt, notifications, quiet }, ...(!editing ? { project_id: project, session_id: session } : {}) })
        .then(async () => { setEditing(null); await refresh(); }).catch(showError).finally(() => setBusy(false));
    }}>
      <label>{tr('Assistant name', 'Nombre del asistente')}<input className="fs-field" required maxLength={120} value={name} disabled={busy} onChange={e => setName(e.target.value)} /></label>
      <label>{tr('Usage notes', 'Notas de uso')}<textarea className="fs-field" rows={2} maxLength={1000} value={description} disabled={busy} onChange={e => setDescription(e.target.value)} /></label>
      <label>{tr('Working instructions', 'Instrucciones de trabajo')}<textarea className="fs-field" rows={5} maxLength={30000} value={instructions} disabled={busy} onChange={e => setInstructions(e.target.value)} /></label>
      <p className="fs-set__help">{tr('Role, responsibilities and working preferences. Applied to each chat turn and proactive run, alongside project instructions.', 'Rol, responsabilidades y preferencias de trabajo. Se aplican a cada turno del chat y ejecución proactiva, junto a las instrucciones del proyecto.')}</p>
      <details className="fs-assistants__detail"><summary>{tr('Selected skills', 'Skills seleccionadas')} · {skills.length}</summary>
        <p className="fs-set__help">{tr('Published procedures to use on every turn. Automatic skill selection and active project skills remain available.', 'Procedimientos publicados que se incluyen en cada turno. También están disponibles la selección automática y las skills activas del proyecto.')}</p>
        <label>{tr('Find a skill', 'Buscar una skill')}<input className="fs-field" type="search" value={skillQuery} disabled={busy} onChange={e => setSkillQuery(e.target.value)} /></label>
        <div className="fs-assistants__skills">{catalog.filter(k => (k.name + ' ' + k.description).toLowerCase().includes(skillQuery.toLowerCase())).map(k => <label key={k.name} className="fs-assistants__check"><input type="checkbox" checked={skills.includes(k.name)} disabled={busy} onChange={e => setSkills(s => e.target.checked ? [...s, k.name] : s.filter(n => n !== k.name))} /><span><strong>{k.name}</strong><small>{k.description}</small></span></label>)}
          {skills.filter(n => !catalog.some(k => k.name === n)).map(n => <label key={n} className="fs-assistants__check"><input type="checkbox" checked disabled={busy} onChange={() => setSkills(s => s.filter(k => k !== n))} /><span>{n} — {tr('unavailable; uncheck to remove', 'no disponible; desmarca para quitar')}</span></label>)}
          {!catalog.length && <p className="fs-set__help">{tr('No published skills available. Manage skills in Personal → Skills.', 'No hay skills publicadas disponibles. Gestiónalas en Personal → Skills.')}</p>}
        </div>
      </details>
      <label className="fs-assistants__check"><input type="checkbox" checked={proactivity.enabled} disabled={busy} onChange={e => changeProactivity({ enabled: e.target.checked })} /><span>{tr('Enable proactivity', 'Activar proactividad')}</span></label>
      {proactivity.enabled && <div className="fs-assistants__proactivity">
        <label>{tr('Proactive mission', 'Misión proactiva')}<textarea className="fs-field" rows={3} required maxLength={30000} value={proactivity.prompt} disabled={busy} onChange={e => changeProactivity({ prompt: e.target.value })} /></label>
        <div className="fs-assistants__fields"><label>{tr('Repeat', 'Repetir')}<select className="fs-field" value={proactivity.schedule} disabled={busy} onChange={e => changeProactivity({ schedule: e.target.value })}>{[['hourly', tr('Hourly', 'Cada hora')], ['daily', tr('Daily', 'Cada día')], ['weekly', tr('Weekly', 'Cada semana')], ['cron', tr('Custom schedule (cron)', 'Horario personalizado (cron)')]].map(([v, label]) => <option key={v} value={v}>{label}</option>)}</select></label>
          <label>{tr('Timezone', 'Zona horaria')}<input className="fs-field" required value={proactivity.timezone} disabled={busy} onChange={e => changeProactivity({ timezone: e.target.value })} /></label>
          {proactivity.schedule !== 'cron' && <label>{proactivity.schedule === 'hourly' ? tr('Minute within each hour', 'Minuto de cada hora') : tr('Local time', 'Hora local')}<input className="fs-field" type="time" required value={proactivity.time} disabled={busy} onChange={e => changeProactivity({ time: e.target.value })} /></label>}
          {proactivity.schedule === 'weekly' && <label>{tr('Weekday', 'Día de la semana')}<select className="fs-field" value={proactivity.day} disabled={busy} onChange={e => changeProactivity({ day: Number(e.target.value) })}>{[tr('Monday', 'Lunes'), tr('Tuesday', 'Martes'), tr('Wednesday', 'Miércoles'), tr('Thursday', 'Jueves'), tr('Friday', 'Viernes'), tr('Saturday', 'Sábado'), tr('Sunday', 'Domingo')].map((d, i) => <option key={i} value={i}>{d}</option>)}</select></label>}
          {proactivity.schedule === 'cron' && <label>{tr('Cron expression', 'Expresión cron')}<input className="fs-field" required value={proactivity.cron} disabled={busy} onChange={e => changeProactivity({ cron: e.target.value })} /><small>{tr('Minute hour day month weekday', 'Minuto hora día mes día de la semana')}</small></label>}
        </div>
        <label className="fs-assistants__check"><input type="checkbox" checked={proactivity.notifications} disabled={busy} onChange={e => changeProactivity({ notifications: e.target.checked })} /><span>{tr('Notify when a run finishes', 'Notificar al terminar una ejecución')}</span></label>
        <label className="fs-assistants__check"><input type="checkbox" checked={proactivity.quiet} disabled={busy} onChange={e => changeProactivity({ quiet: e.target.checked })} /><span>{tr('Stay quiet when there are no meaningful updates', 'Guardar silencio cuando no haya novedades relevantes')}</span></label>
        <p className="fs-set__help">{tr('Results return to this chat. Scheduled work runs while Faustus is running; turning this off pauses future runs.', 'Los resultados vuelven a este chat. La programación se ejecuta mientras Faustus está en marcha; desactivar esta opción pausa las próximas ejecuciones.')}</p>
      </div>}
      {!!proactivity.runs?.length && <details className="fs-assistants__detail"><summary>{tr('Recent activity', 'Actividad reciente')}</summary><ul className="fs-assistants__runs">{proactivity.runs.map(r => <li key={r.id}><strong>{({ success: tr('Completed', 'Completada'), skipped: tr('No updates', 'Sin novedades'), error: tr('Failed', 'Error'), aborted: tr('Stopped', 'Detenida'), running: tr('Working', 'Trabajando'), queued: tr('Queued', 'En cola') })[r.status] || r.status}</strong> · <time dateTime={r.started_at}>{date(r.started_at)}</time>{(r.error || r.result) && <p>{r.error || r.result}</p>}</li>)}</ul></details>}
      {!editing && <div className="fs-assistants__fields"><label>{tr('Project', 'Proyecto')}<select className="fs-field" value={project} disabled={busy || !!session} onChange={e => setProject(e.target.value)}><option value="">{tr('No project', 'Sin proyecto')}</option>{projects.filter(p => !p.archived).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <label>{tr('Conversation', 'Conversación')}<select className="fs-field" value={session} disabled={busy} onChange={e => setSession(e.target.value)}><option value="">{tr('Create a new chat', 'Crear un chat nuevo')}</option>{sessions.map(s => <option key={s.id} value={s.id}>{s.name || s.id}</option>)}</select></label></div>}
      <div className="fs-assistants__actions"><Button type="submit" label={tr('Save assistant', 'Guardar asistente')} disabled={busy || !name.trim()} loading={busy} /><Button label={tr('Cancel', 'Cancelar')} variant="ghost" disabled={busy} onClick={() => setEditing(null)} /></div>
    </form>}
    {!loaded && !error && <p role="status">{tr('Loading assistants…', 'Cargando asistentes…')}</p>}
    {loaded && !assistants.length && <p className="fs-set__help">{tr('Create an assistant or keep an existing chat here. Its history stays in Faustus.', 'Crea un asistente o guarda aquí un chat existente. Su historial permanece en Faustus.')}</p>}
    <ul className="fs-assistants__list">{assistants.map(a => <li key={a.id}>
      <div className="fs-assistants__body"><div className="fs-assistants__heading">{a.state === 'unavailable' ? <strong>{a.name}</strong> : <Link to={'/studio?s=' + encodeURIComponent(a.session_id)}>{a.name}</Link>}<span role="status" data-working={a.state === 'running'}>{stateLabel(a.state)}</span></div>
        {a.description && <p>{a.description}</p>}{a.proactivity?.enabled && <p className="fs-set__help">{tr('Proactive · Next run:', 'Proactivo · Próxima ejecución:')} {date(a.proactivity.next_run)}</p>}{a.proactivity?.last_run && <p className="fs-set__help">{tr('Last run:', 'Última ejecución:')} {date(a.proactivity.last_run)}</p>}{a.preview && <p className="fs-assistants__preview">{a.preview}</p>}
      </div><div className="fs-assistants__actions">{a.proactivity?.enabled && <IconButton icon={Play} label={tr('Run proactive mission now', 'Ejecutar misión proactiva ahora')} disabled={busy || a.state !== 'ready'} onClick={() => void run(a.id)} />}<IconButton icon={Pencil} label={tr('Edit assistant', 'Editar asistente')} disabled={busy || a.state === 'unavailable'} onClick={() => void begin(a)} /><IconButton icon={X} label={tr('Remove assistant; keep chat', 'Quitar asistente; conservar chat')} disabled={busy} onClick={() => void remove(a.id)} /></div>
    </li>)}</ul>
    {error && <p role="alert">{error}</p>}
  </section>;
}
