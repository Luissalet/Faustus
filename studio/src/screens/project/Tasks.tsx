import { Plus, RefreshCw, Search } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, Dialog, IconButton, Skeleton } from '../../components';
import type { Project } from '../../adapters/projects';
import { createProjectTask, editProjectTask, listProjectTasks, prepareTasks, readProjectTask, taskColumns, taskStatusLabel, type ProjectTask, type TaskDetail } from '../../adapters/projectTasks';
import { t } from '../../i18n';

export function TaskEditor({ projectId, taskId, onClose, onSaved }: {
  projectId: string; taskId: number | 'new'; onClose: () => void; onSaved: () => void;
}) {
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [draft, setDraft] = useState({ title: '', body: '', acceptance: '', priority: 2, owner: '', note: '' });
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
      setDraft({ title: value.task.title, body: value.task.body || '', acceptance: value.task.acceptance || '', priority: value.task.priority ?? 2, owner: value.task.owner || '', note: '' });
      setError('');
    }).catch(failure => { if (!controller.signal.aborted) setError(failure.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [projectId, taskId, revision]);
  const save = async () => {
    setSaving(true); setError('');
    try {
      if (taskId === 'new') {
        await createProjectTask(projectId, { ...draft, owner: draft.owner || null, request_id: requestId });
      } else if (detail) {
        await editProjectTask(projectId, taskId, { ...draft, owner: draft.owner || null, expected_etag: detail.task.etag });
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
            <label className="fs-field-label">{t('Assignee')}<input className="fs-field" placeholder={t('Unassigned')} value={draft.owner} maxLength={41} onChange={event => setDraft({ ...draft, owner: event.target.value })} /></label>
            {detail && <p className="fs-muted">{t('Status')}: {t(taskStatusLabel[detail.task.status] || detail.task.status)}{detail.task.reviewer ? ` · ${t('Reviewer')}: ${detail.task.reviewer}` : ''}</p>}
          </div>
          <label className="fs-field-label">{t('Description and objective')}<textarea className="fs-field" rows={8} maxLength={200000} value={draft.body} onChange={event => setDraft({ ...draft, body: event.target.value })} /></label>
          <label className="fs-field-label">{t('Acceptance criteria')}<textarea className="fs-field" rows={4} maxLength={200000} value={draft.acceptance} onChange={event => setDraft({ ...draft, acceptance: event.target.value })} /></label>
          <label className="fs-field-label">{t('Add clarification')}<textarea className="fs-field" rows={3} maxLength={20000} value={draft.note} onChange={event => setDraft({ ...draft, note: event.target.value })} /></label>
          <div className="fs-project-workspace__toolbar"><Button type="submit" label={t('Save changes')} loading={saving} disabled={!draft.title.trim()} /><Button variant="ghost" label={t('Cancel')} onClick={onClose} /></div>
        </fieldset>
      </form>}
      {error && <p role="alert" className="fs-notice">{error} {taskId !== 'new' && <Button variant="ghost" label={t('Reload task')} onClick={() => setRevision(value => value + 1)} />}</p>}
      {detail && <section className="fs-project-task-history" aria-label={t('History and clarifications')}><h3>{t('History and clarifications')}</h3>
        {detail.task.origin_snapshot && <details><summary>{t('Previous history and evidence')}</summary><pre className="fs-project-workspace__source">{JSON.stringify(detail.task.origin_snapshot, null, 2)}</pre></details>}
        {detail.edits?.slice().reverse().map(edit => <details key={edit.revision}><summary>#{edit.revision} · {edit.author} · {new Date(edit.created * 1000).toLocaleString()}</summary>
          {Object.entries(edit.after).filter(([key, value]) => !edit.before || edit.before[key] !== value).map(([key, value]) => <article key={key}>
            <strong>{t(({title: 'Title', body: 'Description and objective', acceptance: 'Acceptance criteria', priority: 'Priority', owner: 'Assignee'} as Record<string, string>)[key] || key)}</strong>
            {edit.before && <p>{t('Before')}: {String(edit.before[key] ?? '')}</p>}<p>{t('After')}: {String(value ?? '')}</p>
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
  const visible = tasks?.filter(task => `${task.id} ${task.title} ${task.body} ${task.acceptance}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()) && (!owner || task.owner === owner));
  return <div className="fs-project-workspace fs-project-tasks" data-testid="project-tasks">
    <header className="fs-project-tasks-toolbar">
      <label className="fs-project-tasks-search"><Search size={16} aria-hidden="true" /><input className="fs-field" aria-label={t('Search tasks')} placeholder={t('Search tasks')} value={query} onChange={event => setQuery(event.target.value)} /></label>
      <select className="fs-field" aria-label={t('Assignee')} value={owner} onChange={event => setOwner(event.target.value)}><option value="">{t('All assignees')}</option>{[...new Set(tasks?.map(task => task.owner).filter(Boolean))].sort().map(value => <option key={value} value={value!}>{value}</option>)}</select>
      <IconButton icon={RefreshCw} label={t('Refresh')} onClick={() => setTick(value => value + 1)} />
      <Button icon={Plus} label={t('New task')} onClick={() => setEditing('new')} />
    </header>
    {error && <p className="fs-notice" role="alert">{error}</p>}
    {migrationError && <p className="fs-notice" role="alert">{t('Some previous records could not be imported')}: {migrationError} <Button variant="ghost" label={t('Retry')} onClick={() => setTick(value => value + 1)} /></p>}
    {!tasks && !error && <Skeleton label={t('Loading the board')} count={3} height="80px" />}
    {tasks && <div className="fs-project-task-columns">
      {taskColumns.map(column => {
        const rows = visible!.filter(task => (column.statuses as readonly string[]).includes(task.status));
        return <section className="fs-project-task-column" key={column.id} data-column={column.id} aria-label={t(column.label)}><h2>{t(column.label)} <span>{rows.length}</span></h2>
          <ul>{rows.map(task => <li key={task.id}><button type="button" className="fs-project-task-card" data-testid="project-task-card" data-status={task.status} onClick={() => setEditing(task.id)}>
            <span className="fs-project-task-card-meta">#{task.id} · P{task.priority} · {task.owner || t('Unassigned')}</span><strong>{task.title}</strong>
            <span className="fs-project-task-card-status">{t(taskStatusLabel[task.status] || task.status)}</span>
          </button></li>)}</ul>{!rows.length && <p className="fs-muted">{t('Nothing here.')}</p>}</section>;
      })}
    </div>}
    {editing !== null && <TaskEditor key={editing} projectId={project.id} taskId={editing} onClose={() => setEditing(null)} onSaved={() => setTick(value => value + 1)} />}
  </div>;
}
