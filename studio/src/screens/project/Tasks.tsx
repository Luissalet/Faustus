import { Download, LayoutGrid, Plus, RefreshCw, Search, Table2, Upload } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, Dialog, IconButton, Skeleton } from '../../components';
import type { Project } from '../../adapters/projects';
import { createProjectTask, editProjectTask, listProjectTasks, prepareTasks, readProjectTask, taskColumns, taskStatusLabel, type ProjectTask, type TaskDetail } from '../../adapters/projectTasks';
import { t } from '../../i18n';
import { ImportDialog } from '../board/ImportDialog';
import { exportMdUrl } from '../../adapters/board';

export function TaskEditor({ projectId, taskId, onClose, onSaved }: {
  projectId: string; taskId: number | 'new'; onClose: () => void; onSaved: () => void;
}) {
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [draft, setDraft] = useState({ title: '', body: '', acceptance: '', priority: 2, owner: '', note: '', status: 'open', kind: 'feature', labels: '' });
  const [loading, setLoading] = useState(taskId !== 'new');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [requestId] = useState(() => crypto.randomUUID());
  useEffect(() => {
    if (taskId === 'new') return;
    const controller = new AbortController();
    setLoading(true);
    readProjectTask(projectId, taskId, controller.signal).then(value => {
      if (controller.signal.aborted) return;
      setDetail(value);
      setDraft({ title: value.task.title, body: value.task.body || '', acceptance: value.task.acceptance || '', priority: value.task.priority ?? 2, owner: value.task.owner || '', note: '', status: value.task.status, kind: value.task.kind || 'feature', labels: (value.task.labels || []).join(', ') });
      setError('');
    }).catch(failure => { if (!controller.signal.aborted) setError(failure.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [projectId, taskId, revision]);
  const save = async () => {
    setSaving(true); setError('');
    try {
      if (taskId === 'new') {
        const {status: _status, ...creation} = draft;
        await createProjectTask(projectId, { ...creation, labels: draft.labels.split(',').map(v => v.trim()).filter(Boolean), owner: draft.owner || null, request_id: requestId });
      } else if (detail) {
        await editProjectTask(projectId, taskId, { ...draft, labels: draft.labels.split(',').map(v => v.trim()).filter(Boolean), owner: draft.owner || null, expected_etag: detail.task.etag });
      }
      onSaved(); onClose();
    } catch (failure) { setError((failure as Error).message); }
    finally { setSaving(false); }
  };
  return <Dialog open onOpenChange={open => { if (!open && !saving) onClose(); }} title={taskId === 'new' ? t('New task') : t('Task #{id}', { id: taskId })}
    className="fs-project-task-dialog" testId="project-task-editor">
    {loading ? <Skeleton label={t('Loading task')} count={4} height="44px" /> : <>
      {(taskId === 'new' || detail) && <form className="fs-project-task-form" onSubmit={event => { event.preventDefault(); void save(); }}>
        <fieldset disabled={saving}>
          <label className="fs-field-label">{t('Title')}<input className="fs-field" autoFocus maxLength={300} required value={draft.title} onChange={event => setDraft({ ...draft, title: event.target.value })} /></label>
          <div className="fs-project-task-fields">
            <label className="fs-field-label">{t('Priority')}<select className="fs-field" value={draft.priority} onChange={event => setDraft({ ...draft, priority: Number(event.target.value) })}>
              {[0, 1, 2, 3].map(value => <option key={value} value={value}>P{value} · {t(['Urgent', 'High', 'Medium', 'Low'][value])}</option>)}</select></label>
            <label className="fs-field-label">{t('Assignee')}<input className="fs-field" placeholder={t('Unassigned')} value={draft.owner} maxLength={120} onChange={event => setDraft({ ...draft, owner: event.target.value })} /></label>
            {detail && <label className="fs-field-label">{t('Status')}<select className="fs-field" value={draft.status} onChange={event => setDraft({...draft, status: event.target.value})}>{[...new Set([draft.status, 'open', 'in_progress', 'blocked', 'done', 'dropped'])].map(value => <option key={value} value={value}>{t(taskStatusLabel[value] || value)}</option>)}</select></label>}
            <label className="fs-field-label">{t('Type')}<select className="fs-field" value={draft.kind} onChange={event => setDraft({...draft, kind: event.target.value})}>{['feature','bug','task','idea','chore','research','review','eval','docs'].map(value => <option key={value} value={value}>{t(value)}</option>)}</select></label>
            <label className="fs-field-label">{t('Labels (comma-separated, optional)')}<input className="fs-field" value={draft.labels} onChange={event => setDraft({...draft, labels: event.target.value})} /></label>
          </div>
          <label className="fs-field-label">{t('Description and objective')}<textarea className="fs-field" rows={8} maxLength={200000} value={draft.body} onChange={event => setDraft({ ...draft, body: event.target.value })} /></label>
          <label className="fs-field-label">{t('Acceptance criteria')}<textarea className="fs-field" rows={4} maxLength={200000} value={draft.acceptance} onChange={event => setDraft({ ...draft, acceptance: event.target.value })} /></label>
          <label className="fs-field-label">{t('Add clarification')}<textarea className="fs-field" rows={3} maxLength={200000} value={draft.note} onChange={event => setDraft({ ...draft, note: event.target.value })} /></label>
          <div className="fs-project-workspace__toolbar"><Button type="submit" label={t('Save changes')} loading={saving} disabled={!draft.title.trim()} /><Button variant="ghost" label={t('Cancel')} onClick={onClose} /></div>
        </fieldset>
      </form>}
      {error && <p role="alert" className="fs-notice">{error} {taskId !== 'new' && <Button variant="ghost" label={t('Reload task')} onClick={() => setRevision(value => value + 1)} />}</p>}
      {detail && <section className="fs-project-task-history" aria-label={t('History and clarifications')}><h3>{t('History and clarifications')}</h3>
        {detail.task.origin_snapshot && <details><summary>{t('Previous history and evidence')}</summary><pre className="fs-project-workspace__source">{JSON.stringify(detail.task.origin_snapshot, null, 2)}</pre></details>}
        {detail.edits?.slice().reverse().map(edit => <details key={edit.revision}><summary>#{edit.revision} · {edit.author} · {new Date(edit.created * 1000).toLocaleString()}</summary>
          {Object.entries(edit.after).filter(([key, value]) => !edit.before || edit.before[key] !== value).map(([key, value]) => <article key={key}>
            <strong>{t(({title: 'Title', body: 'Description and objective', acceptance: 'Acceptance criteria', priority: 'Priority', owner: 'Assignee', kind: 'Type', status: 'Status', labels: 'Labels'} as Record<string, string>)[key] || key)}</strong>
            {edit.before && <p>{t('Before')}: {typeof edit.before[key] === 'object' ? JSON.stringify(edit.before[key]) : String(edit.before[key] ?? '')}</p>}<p>{t('After')}: {typeof value === 'object' ? JSON.stringify(value) : String(value ?? '')}</p>
          </article>)}
        </details>)}
        {detail.messages.map(message => <article key={message.id}><header><strong>{message.author}</strong> · <time>{new Date(message.created * 1000).toLocaleString()}</time></header><p>{message.body}</p></article>)}
      </section>}
    </>}
  </Dialog>;
}

export function ProjectTasks({ project }: { project: Project; focus?: string; say: (message: string) => void }) {
  const [tasks, setTasks] = useState<ProjectTask[] | null>(null);
  const [error, setError] = useState('');
  const [migrationError, setMigrationError] = useState('');
  const [query, setQuery] = useState('');
  const [owner, setOwner] = useState('');
  const [kind, setKind] = useState('');
  const [priority, setPriority] = useState('');
  const [label, setLabel] = useState('');
  const [table, setTable] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [editing, setEditing] = useState<number | 'new' | null>(() => {
    const raw = new URLSearchParams(window.location.search).get('task');
    return raw && /^\d+$/.test(raw) ? Number(raw) : null;
  });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const load = () => listProjectTasks(project.id, controller.signal).then(result => {
      if (!controller.signal.aborted) { setTasks(result.tasks); setError(''); }
    }).catch(failure => { if (!controller.signal.aborted) setError(failure.message); });
    void load();
    setMigrationError('');
    void prepareTasks(project.id).catch(failure => { if (!controller.signal.aborted) setMigrationError(failure.message); })
      .finally(() => { if (!controller.signal.aborted) void load(); });
    const timer = window.setInterval(() => { void load(); }, 8000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [project.id, tick]);
  const visible = tasks?.filter(task => `${task.id} ${task.origin_ref || ''} ${task.title} ${task.body} ${task.acceptance} ${task.labels?.join(' ')}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()) && (!owner || task.owner === owner) && (!kind || task.kind === kind) && (priority === '' || String(task.priority) === priority) && (!label || task.labels?.includes(label)));
  const drop = async (event: React.DragEvent, column: string) => {
    event.preventDefault();
    const task = tasks?.find(value => value.id === Number(event.dataTransfer.getData('text/plain')));
    if (!task || column === 'review') return;
    try { await editProjectTask(project.id, task.id, {expected_etag: task.etag, status: column}); setTick(value => value + 1); }
    catch (failure) { setError((failure as Error).message); }
  };
  return <div className="fs-project-workspace fs-project-tasks" data-testid="project-tasks">
    <header className="fs-project-tasks-toolbar">
      <label className="fs-project-tasks-search"><Search size={16} aria-hidden="true" /><input className="fs-field" aria-label={t('Search tasks')} placeholder={t('Search tasks')} value={query} onChange={event => setQuery(event.target.value)} /></label>
      <select className="fs-field" aria-label={t('Assignee')} value={owner} onChange={event => setOwner(event.target.value)}><option value="">{t('All assignees')}</option>{[...new Set(tasks?.map(task => task.owner).filter(Boolean))].sort().map(value => <option key={value} value={value!}>{value}</option>)}</select>
      <select className="fs-field" aria-label={t('Type')} value={kind} onChange={event => setKind(event.target.value)}><option value="">{t('Any type')}</option>{[...new Set(tasks?.map(task => task.kind))].sort().map(value => <option key={value} value={value}>{value}</option>)}</select>
      <select className="fs-field" aria-label={t('Priority')} value={priority} onChange={event => setPriority(event.target.value)}><option value="">{t('Any priority')}</option>{[0,1,2,3].map(value => <option key={value} value={value}>P{value}</option>)}</select>
      <select className="fs-field" aria-label={t('Labels')} value={label} onChange={event => setLabel(event.target.value)}><option value="">{t('All labels')}</option>{[...new Set(tasks?.flatMap(task => task.labels || []))].sort().map(value => <option key={value} value={value}>{value}</option>)}</select>
      <Button variant="ghost" icon={table ? LayoutGrid : Table2} label={t(table ? 'Kanban' : 'Table')} onClick={() => setTable(value => !value)} />
      <Button variant="ghost" icon={Upload} label={t('Import…')} onClick={() => setImportOpen(true)} />
      <a className="fs-btn" data-variant="ghost" href={exportMdUrl(project.id)} target="_blank" rel="noreferrer"><Download size={16} />{t('Export .md')}</a>
      <IconButton icon={RefreshCw} label={t('Refresh')} onClick={() => setTick(value => value + 1)} />
      <Button icon={Plus} label={t('New task')} onClick={() => setEditing('new')} />
    </header>
    {error && <p className="fs-notice" role="alert">{error}</p>}
    {migrationError && <p className="fs-notice" role="alert">{t('Some previous records could not be imported')}: {migrationError} <Button variant="ghost" label={t('Retry')} onClick={() => setTick(value => value + 1)} /></p>}
    {!tasks && !error && <Skeleton label={t('Loading the board')} count={3} height="80px" />}
    {tasks && table && <div className="fs-project-task-table"><table><thead><tr>{['Task','Type','Priority','Status','Assignee','Labels'].map(value => <th key={value}>{t(value)}</th>)}</tr></thead><tbody>{visible!.map(task => <tr key={task.id}><td><button type="button" onClick={() => setEditing(task.id)}>#{task.id} {task.title}</button></td><td>{task.kind}</td><td>P{task.priority}</td><td>{t(taskStatusLabel[task.status] || task.status)}</td><td>{task.owner}</td><td>{task.labels?.join(', ')}</td></tr>)}</tbody></table></div>}
    {tasks && !table && <div className="fs-project-task-columns">
      {taskColumns.map(column => {
        const rows = visible!.filter(task => (column.statuses as readonly string[]).includes(task.status));
        return <section className="fs-project-task-column" key={column.id} data-column={column.id} aria-label={t(column.label)} onDragOver={event => { if (column.id !== 'review') event.preventDefault(); }} onDrop={event => void drop(event, column.id)}><h2>{t(column.label)} <span>{rows.length}</span></h2>
          <ul>{rows.map(task => <li key={task.id}><button type="button" draggable onDragStart={event => event.dataTransfer.setData('text/plain', String(task.id))} className="fs-project-task-card" data-testid="project-task-card" data-status={task.status} onClick={() => setEditing(task.id)}>
            <span className="fs-project-task-card-meta">#{task.id} · P{task.priority} · {task.owner || t('Unassigned')} · {task.kind}</span><strong>{task.title}</strong>
            {!!task.labels?.length && <span className="fs-project-task-card-meta">{task.labels.join(' · ')}</span>}
            <span className="fs-project-task-card-status">{t(taskStatusLabel[task.status] || task.status)}</span>
          </button></li>)}</ul>{!rows.length && <p className="fs-muted">{t('Nothing here.')}</p>}</section>;
      })}
    </div>}
    {editing !== null && <TaskEditor key={editing} projectId={project.id} taskId={editing} onClose={() => setEditing(null)} onSaved={() => setTick(value => value + 1)} />}
    <ImportDialog open={importOpen} onOpenChange={setImportOpen} projectId={project.id} onImported={() => setTick(value => value + 1)} />
  </div>;
}
