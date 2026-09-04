import { Activity as ActivityIcon } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';
import { EmptyState, Skeleton, StatusBadge, type RunStatus } from '../components';
import { duration, loadActivity, type ActivityRun } from '../adapters/activity';
import { relativeTime } from '../adapters/home';
import './projects.css';
import './home.css';
import './activity.css';

const FILTERS: { id: string; label: string; match: (run: ActivityRun) => boolean }[] = [
  { id: 'todo', label: 'Todo', match: () => true },
  { id: 'accion', label: 'Requiere acción', match: (run) => run.status === 'waiting' },
  { id: 'activo', label: 'En curso', match: (run) => run.status === 'running' },
  { id: 'fallido', label: 'Fallidos', match: (run) => run.status === 'failed' },
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
        title="No he podido leer la actividad"
        body="Ninguno de los subsistemas ha respondido. La interfaz anterior no depende de esta pantalla."
        primaryAction={{
          label: 'Abrir la interfaz anterior',
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
          <h1 className="fs-screen__title">Actividad</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {waiting > 0
              ? `${waiting} ${waiting === 1 ? 'cosa espera' : 'cosas esperan'} una decisión tuya.`
              : 'Tareas, renders y aprobaciones, con el mismo lenguaje para todos.'}
          </p>
        </div>
      </header>

      <div className="fs-tabs" role="tablist" aria-label="Filtrar actividad">
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
            {entry.label}
          </button>
        ))}
      </div>

      {!runs && <Skeleton label="Cargando la actividad" count={6} height="52px" />}

      {runs && visible.length === 0 && (
        <EmptyState
          icon={ActivityIcon}
          title={filterId === 'todo' ? 'Nada ha corrido todavía' : 'Nada en este estado'}
          body={
            filterId === 'todo'
              ? 'Cuando una tarea, un render o un agente hagan algo, aparecerá aquí con su estado y lo que tardó.'
              : 'Prueba con otro filtro: el que has elegido no tiene nada ahora mismo.'
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
                {run.kind}
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
