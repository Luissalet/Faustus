import { useEffect, useState } from 'react';
import { getJson, responseReason } from '../../adapters/api';
import { locale } from '../../i18n';
import './coworkers.css';

type Coworker = { id?: string; revision?: number; name: string; agent: string; responsibility: string; notes: string; hoards: string[]; state: 'active' | 'paused' | 'archived' };
type Run = { request_id: string; mission: string; state: string; receipt?: unknown };
const blank: Coworker = { name: '', agent: 'implementer', responsibility: '', notes: '', hoards: [], state: 'active' };

export function CoworkersPanel() {
  const es = locale().startsWith('es');
  const word = (a: string, b: string) => es ? a : b;
  const [rows, setRows] = useState<Coworker[]>([]);
  const [templates, setTemplates] = useState<Coworker[]>([]);
  const [templateQuery, setTemplateQuery] = useState('');
  const [templateHoard, setTemplateHoard] = useState('');
  const [draft, setDraft] = useState<Coworker | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [checkedRuns, setCheckedRuns] = useState<Record<string, boolean>>({});
  const availableHoards = [...new Set(templates.flatMap(row => row.hoards))].sort();
  const filteredTemplates = templates.filter(row => (!templateHoard || row.hoards.includes(templateHoard)) &&
    `${row.name} ${row.responsibility} ${row.hoards.join(' ')}`.toLocaleLowerCase().includes(templateQuery.toLocaleLowerCase()));
  const load = async () => {
    const data = await getJson<{ coworkers: Coworker[]; templates: Coworker[] }>('/api/coworkers');
    setRows(data.coworkers); setTemplates(data.templates);
  };
  useEffect(() => { void load().catch(e => setError(String(e))); }, []);
  const inspect = async (row: Coworker) => {
    try {
      const data = await getJson<{ coworker: Coworker; runs: Run[] }>(`/api/coworkers/${row.id}`);
      setDraft(data.coworker); setRuns(data.runs); setError('');
    } catch (e) { setError(String(e)); }
  };
  const save = async () => {
    if (!draft) return;
    setBusy(true); setError('');
    try {
      const { id, revision, ...coworker } = draft;
      const response = await fetch('/api/coworkers', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ coworker, id, expected_revision: revision }) });
      if (!response.ok) throw new Error(await responseReason(response, '/api/coworkers'));
      setDraft(await response.json()); await load();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  return <section className="fs-coworkers" aria-label={word('Compañeros persistentes', 'Persistent coworkers')}>
    <header><h2>{word('Compañeros', 'Coworkers')}</h2><button onClick={() => { setDraft({ ...blank }); setRuns([]); }}>{word('Crear compañero', 'Create coworker')}</button></header>
    <p>{word('Guarda responsabilidades y contexto para volver a trabajar con el mismo compañero. Sus encargos se ejecutan desde el chat con los permisos del agente elegido.', 'Save responsibilities and context to work with the same coworker again. Missions run from chat under the selected agent’s permissions.')}</p>
    {error && <p role="alert">{error}</p>}
    <div className="fs-coworkers__list">{rows.map(row => <button key={row.id} onClick={() => void inspect(row)}>{row.name} · {word(row.state === 'active' ? 'activo' : row.state === 'paused' ? 'pausado' : 'archivado', row.state)}</button>)}</div>
    {!draft && <div>
      <h3>{word('Elegir especialidad', 'Choose a specialty')}</h3>
      <div className="fs-coworkers__filters">
        <label>{word('Buscar especialidad', 'Find a specialty')}<input type="search" value={templateQuery} onChange={e => setTemplateQuery(e.target.value)} placeholder={word('Diseño, IA, contactos…', 'Design, AI, contacts…')} /></label>
        <label>{word('Filtrar por Hoard', 'Filter by Hoard')}<select value={templateHoard} onChange={e => setTemplateHoard(e.target.value)}><option value="">{word('Todos', 'All')}</option>{availableHoards.map(id => <option key={id} value={id}>{id}</option>)}</select></label>
      </div>
      <p role="status">{filteredTemplates.length} {word('especialidades disponibles', 'available specialties')}</p>
      <div className="fs-coworkers__list">{filteredTemplates.map(row => <button key={row.name} onClick={() => { setDraft({ ...blank, ...row }); setRuns([]); }}>{word('Crear: ', 'Create: ')}{row.name}</button>)}</div>
      {!filteredTemplates.length && <p>{word('No hay coincidencias. Cambia el texto o el Hoard para ver otras especialidades.', 'No matches. Change the search or Hoard to see other specialties.')}</p>}
    </div>}
    {draft && <form onSubmit={e => { e.preventDefault(); void save(); }}>
      <label>{word('Nombre', 'Name')}<input required maxLength={100} value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
      <label>{word('Agente', 'Agent')}<input required pattern="[a-zA-Z0-9_-]{1,80}" value={draft.agent} onChange={e => setDraft({ ...draft, agent: e.target.value })} /></label>
      <label>{word('Responsabilidad', 'Responsibility')}<textarea required maxLength={8000} rows={4} value={draft.responsibility} onChange={e => setDraft({ ...draft, responsibility: e.target.value })} /></label>
      <label>{word('Contexto guardado por ti', 'Context maintained by you')}<textarea maxLength={16000} rows={4} value={draft.notes} onChange={e => setDraft({ ...draft, notes: e.target.value })} /></label>
      <label>Hoards<input value={draft.hoards.join(', ')} onChange={e => setDraft({ ...draft, hoards: e.target.value.split(',').map(v => v.trim()).filter(Boolean) })} /></label>
      <label>{word('Estado', 'State')}<select value={draft.state} onChange={e => setDraft({ ...draft, state: e.target.value as Coworker['state'] })}><option value="active">{word('Activo', 'Active')}</option><option value="paused">{word('Pausado', 'Paused')}</option><option value="archived">{word('Archivado', 'Archived')}</option></select></label>
      <div className="fs-coworkers__list"><button disabled={busy} type="submit">{word(busy ? 'Guardando…' : 'Guardar', busy ? 'Saving…' : 'Save')}</button><button disabled={busy} type="button" onClick={() => { setDraft(null); setRuns([]); }}>{word('Cerrar editor', 'Close editor')}</button></div>
      {draft.id && <p>{word('Para encargarle trabajo, pide en el chat:', 'To assign work, ask in chat:')} <q>{word(`Encarga a ${draft.name} que…`, `Ask ${draft.name} to…`)}</q></p>}
    </form>}
    {runs.length > 0 && <div><h3>{word('Encargos y resultados', 'Missions and receipts')}</h3>{runs.map(run => <details key={run.request_id}><summary>{run.state} · {run.mission.slice(0, 120)}</summary><pre>{JSON.stringify(run, null, 2)}</pre>{['running','unknown'].includes(run.state) && <div><label><input type="checkbox" checked={!!checkedRuns[run.request_id]} onChange={e=>setCheckedRuns({...checkedRuns,[run.request_id]:e.target.checked})}/>{word('He revisado la sesión del trabajador y el encargo ya no sigue activo.', 'I inspected the worker session and the mission is no longer active.')}</label><button disabled={!checkedRuns[run.request_id] || busy} onClick={async()=>{setBusy(true);setError('');try{const response=await fetch(`/api/coworkers/${draft?.id}/runs/${run.request_id}/close`,{method:'POST',credentials:'same-origin'});if(!response.ok)throw new Error(await responseReason(response,'/api/coworkers'));if(draft)await inspect(draft);}catch(e){setError(String(e));}finally{setBusy(false);}}}>{word('Cerrar registro tras revisar', 'Close record after inspection')}</button></div>}</details>)}</div>}
  </section>;
}
