import { Activity as ActivityIcon } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';
import { EmptyState, Skeleton, StatusBadge, type RunStatus } from '../components';
import { duration, loadActivity, type ActivityRun } from '../adapters/activity';
import { relativeTime } from '../adapters/home';
import './projects.css';
import './home.css';
import './activity.css';
import { t, tn } from '../i18n';

const FILTERS: { id: string; label: string; match: (run: ActivityRun) => boolean }[] = [
  { id: 'todo', label: 'All', match: () => true },
  { id: 'accion', label: 'Needs action', match: (run) => run.status === 'waiting' },
  { id: 'activo', label: 'In progress', match: (run) => run.status === 'running' },
  { id: 'fallido', label: 'Failed', match: (run) => run.status === 'failed' },
];

/**
 * Actividad (UI-050 / UI-051).
 *
 * Every kind of work in one list with one vocabulary. Not a log dump: the
 * raw output, tools and evidence stay in the run's own detail. What belongs
 * here is type, goal, state, when, and whether it is waiting for you.
 */
export function ActivityScreen() {
  const [params, setParams] = useSearchParams();
  const [runs, setRuns] = useState<ActivityRun[] | null>(null);
  const [degraded, setDegraded] = useState<string[]>([]);
  const [failed, setFailed] = useState(false);

  const filterId = params.get('status') ?? 'todo';
  const filter = FILTERS.find((entry) => entry.id === filterId) ?? FILTERS[0];

  useEffect(() => {
    const controller = new AbortController();
    loadActivity(controller.signal)
      .then((result) => {
        setRuns(result.runs);
        setDegraded(result.degraded);
      })
      .catch(() => setFailed(true));
    return () => controller.abort();
  }, []);

  const visible = useMemo(() => (runs ?? []).filter(filter.match), [runs, filter]);
  const waiting = useMemo(
    () => (runs ?? []).filter((run) => run.status === 'waiting').length,
    [runs],
  );

  if (failed) {
    return (
      <EmptyState
        icon={ActivityIcon}
        title={t('Could not read the activity')}
        body={t('None of the subsystems responded. The previous interface does not depend on this screen.')}
        primaryAction={{
          label: t('Open the previous interface'),
          onClick: () => {
            window.location.href = '/?shell=legacy';
          },
        }}
      />
    );
  }

  return (
    <div className="fs-screen" data-testid="activity">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Activity')}</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {waiting > 0
              ? tn(waiting, '{n} thing is waiting for your decision.', '{n} things are waiting for your decision.')
              : t('Tasks, renders and approvals, in the same language for all.')}
          </p>
        </div>
      </header>

      <div className="fs-tabs" role="tablist" aria-label={t('Filter activity')}>
        {FILTERS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={entry.id === filter.id}
            className="fs-tab"
            data-testid={`activity-filter-${entry.id}`}
            onClick={() => {
              const next = new URLSearchParams(params);
              if (entry.id === 'todo') next.delete('status');
              else next.set('status', entry.id);
              setParams(next);
            }}
          >
            {t(entry.label)}
          </button>
        ))}
      </div>

      {!runs && <Skeleton label={t('Loading the activity')} count={6} height="52px" />}

      {runs && visible.length === 0 && (
        <EmptyState
          icon={ActivityIcon}
          title={filterId === 'todo' ? t('Nothing has run yet') : t('Nothing in this state')}
          body={
            filterId === 'todo'
              ? t('When a task, a render or an agent does something, it will appear here with its state and how long it took.')
              : t('Try another filter: the one you chose has nothing right now.')
          }
        />
      )}

      {runs && visible.length > 0 && (
        <div className="fs-list fs-list--rail">
          {visible.map((run) => (
            <article
              className="fs-run"
              key={`${run.kind}-${run.id}`}
              data-state={run.status}
              data-testid="activity-run"
            >
              <span className="fs-run__kind" data-kind={run.kind}>
                {t(run.kind)}
              </span>
              <span className="fs-run__main">
                <span className="fs-row__name">{run.title}</span>
                {run.detail && <span className="fs-run__detail">{run.detail}</span>}
                <span className="fs-row__meta">
                  {[
                    relativeTime(run.startedAt),
                    duration(run.startedAt, run.finishedAt),
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </span>
              <StatusBadge status={run.status as RunStatus} label={run.statusLabel} />
            </article>
          ))}
        </div>
      )}

      {degraded.length > 0 && (
        <p className="fs-notice" data-tone="warning">
          No he podido leer {degraded.join(', ')}. El resto de la lista es real.
        </p>
      )}
    </div>
  );
}
