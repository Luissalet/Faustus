import { Plus, Zap } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, Skeleton } from '../../components';
import { createObjective, dropObjective, getObjectives, OBJECTIVE_STATUSES, objectiveClosed, parseDeps, patchObjective, sortObjectives, type Objective, type Objectives } from '../../adapters/projects';
import { t } from '../../i18n';

/**
 * Objectives: what the project is trying to achieve, so the agent knows
 * when a piece of work counts as done. Status and priority change in
 * place; notes unfold; dropped ones hide unless asked for.
 */
function Row({ o, data, projectId, onChanged, say }: { o: Objective; data: Objectives; projectId: string; onChanged: () => void; say: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  const [notes, setNotes] = useState(o.notes);
  const dropped = o.status === 'dropped';
  const blockers = data.edges.filter((e) => e.from === o.id).map((e) => e.to);
  const hint = data.hints[o.id];
  const patch = async (body: Parameters<typeof patchObjective>[2]) => {
    try {
      await patchObjective(projectId, o.id, body);
      onChanged();
    } catch (e) {
      say((e as Error).message);
    }
  };
  return (
    <li className="fs-pj__obj" data-closed={objectiveClosed(o) || undefined} data-dropped={dropped || undefined}>
      <div className="fs-pj__obj-main">
        <code className="fs-pj__obj-id">{o.id}</code>
        <button type="button" className="fs-pj__obj-title" onClick={() => setOpen((v) => !v)} aria-expanded={open} title={open ? t('Hide notes') : t('Show notes')}>
          {o.title}
        </button>
        {hint && (
          <span className="fs-pj__obj-hint" title={hint}>
            <Zap size={12} aria-hidden="true" />
          </span>
        )}
        <select className="fs-field fs-pj__obj-select" value={o.status} disabled={dropped} onChange={(e) => void patch({ status: e.target.value })} aria-label={t('Status of {id}', { id: o.id })} data-status={o.status}>
          {[...OBJECTIVE_STATUSES, ...(dropped ? [{ value: 'dropped', label: 'dropped' }] : [])].map((s) => (
            <option key={s.value} value={s.value}>
              {t(s.label)}
            </option>
          ))}
        </select>
        <select className="fs-field fs-pj__obj-select" value={o.priority} disabled={dropped} onChange={(e) => void patch({ priority: Number(e.target.value) || 3 })} aria-label={t('Priority of {id}', { id: o.id })}>
          {[1, 2, 3, 4].map((p) => (
            <option key={p} value={p}>
              P{p}
            </option>
          ))}
        </select>
        {!dropped && (
          <button
            type="button"
            className="fs-pj__obj-drop"
            aria-label={t('Drop {id}', { id: o.id })}
            title={t('Drop the objective')}
            onClick={() => {
              void dropObjective(projectId, o.id).then(onChanged, (e: Error) => say(e.message));
            }}
          >
            ×
          </button>
        )}
      </div>
      {blockers.length > 0 && <div className="fs-pj__obj-deps">{t('blocked by {ids}', { ids: blockers.join(', ') })}</div>}
      {open && (
        <div className="fs-pj__obj-notes">
          <textarea className="fs-field fs-pj__textarea" rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder={t('Notes for {id}…', { id: o.id })} spellCheck={false} />
          <Button variant="secondary" size="sm" label={t('Save notes')} onClick={() => void patch({ notes })} />
        </div>
      )}
    </li>
  );
}

export function ProjectObjectives({ projectId, say }: { projectId: string; say: (m: string) => void }) {
  const [data, setData] = useState<Objectives | null>(null);
  const [showDropped, setShowDropped] = useState(false);
  const [title, setTitle] = useState('');
  const [priority, setPriority] = useState(3);
  const [deps, setDeps] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    getObjectives(projectId)
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, [projectId]);
  useEffect(reload, [reload]);

  const add = async () => {
    if (!title.trim()) return;
    setBusy(true);
    try {
      const body: { title: string; priority: number; deps?: string[] } = { title: title.trim(), priority };
      const ids = parseDeps(deps);
      if (ids.length) body.deps = ids;
      await createObjective(projectId, body);
      setTitle('');
      setDeps('');
      reload();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const list = data ? sortObjectives(data.objectives).filter((o) => showDropped || o.status !== 'dropped') : [];
  const droppedCount = data?.objectives.filter((o) => o.status === 'dropped').length ?? 0;

  return (
    <div className="fs-pj__objectives">
      <div className="fs-pj__obj-head">
        <p className="fs-prose">{t('What this project is trying to achieve; the agent uses them to know when a piece of work counts as done.')}</p>
        <label className="fs-switch">
          <input type="checkbox" checked={showDropped} onChange={(e) => setShowDropped(e.target.checked)} />
          <span>{droppedCount ? t('Show dropped ({n})', { n: droppedCount }) : t('Show dropped')}</span>
        </label>
      </div>
      {error && <p className="fs-pj__error">{error}</p>}
      {data === null && !error ? (
        <Skeleton label={t('Loading the objectives')} count={3} height="36px" />
      ) : list.length === 0 ? (
        <p className="fs-pj__muted">{t('No objectives yet.')}</p>
      ) : (
        <ul className="fs-pj__obj-list">
          {list.map((o) => (
            <Row key={o.id} o={o} data={data!} projectId={projectId} onChanged={reload} say={say} />
          ))}
        </ul>
      )}
      <form
        className="fs-pj__obj-add"
        onSubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <input className="fs-field fs-pj__grow" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t('Add an objective…')} maxLength={200} data-testid="objective-title" />
        <select className="fs-field" value={priority} onChange={(e) => setPriority(Number(e.target.value))} aria-label={t('Priority of the new objective')}>
          {[1, 2, 3, 4].map((p) => (
            <option key={p} value={p}>
              P{p}
            </option>
          ))}
        </select>
        <input className="fs-field" value={deps} onChange={(e) => setDeps(e.target.value)} placeholder={t('blocked by (OBJ-1, OBJ-2)')} />
        <Button type="submit" variant="secondary" size="sm" icon={Plus} label={t('Add')} loading={busy} disabled={!title.trim()} testId="objective-add" />
      </form>
      {data && data.log.length > 0 && (
        <details className="fs-pj__obj-log">
          <summary>{t('Objective activity')}</summary>
          <div className="fs-pj__obj-log-rows">
            {data.log
              .slice(-10)
              .reverse()
              .map((e, i) => (
                <div key={i} className="fs-pj__obj-log-row">
                  <time>{e.ts.replace('T', ' ').slice(0, 16)}</time>
                  <span>{e.actor}</span>
                  <span>{e.op ? `${e.kind === 'conflict' ? 'conflict ' : ''}${e.op}` : e.kind}</span>
                  <span>{e.id}</span>
                  {e.why && <em>{e.why}</em>}
                </div>
              ))}
          </div>
        </details>
      )}
    </div>
  );
}
