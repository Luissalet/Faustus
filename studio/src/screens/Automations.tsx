import { Workflow } from 'lucide-react';
import { useEffect, useState } from 'react';
import { EmptyState, Skeleton, StatusBadge } from '../components';
import {
  describeAction,
  describeTrigger,
  listAutomations,
  type Automation,
} from '../adapters/automations';
import { relativeTime } from '../adapters/home';
import './projects.css';
import './home.css';
import './activity.css';
import { t, tn } from '../i18n';

/**
 * Automatizaciones (UI-052).
 *
 * A list of readable recipes, which is what the product document asks for:
 * trigger, what it does, when it runs next, how it went last time. The node
 * editor is an advanced inspection and stays where it is; it does not get to
 * be the first thing you see.
 */
export function AutomationsScreen() {
  const [tasks, setTasks] = useState<Automation[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    listAutomations(controller.signal).then(setTasks).catch(() => setFailed(true));
    return () => controller.abort();
  }, []);

  if (failed) {
    return (
      <EmptyState
        icon={Workflow}
        title={t('Could not read your automations')}
        body={t('The tasks endpoint is not responding. The previous interface does not depend on this screen.')}
        primaryAction={{
          label: t('Open the previous interface'),
          onClick: () => {
            window.location.href = '/?shell=legacy';
          },
        }}
      />
    );
  }

  const active = tasks?.filter((task) => task.status === 'active').length ?? 0;

  return (
    <div className="fs-screen" data-testid="automations">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Automations')}</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {tasks
              ? `${tn(active, '{n} active', '{n} active#')} ${t('of {total}. Each one says when it fires and what it does.', { total: tasks.length })}`
              : t('Each one says when it fires and what it does.')}
          </p>
        </div>
      </header>

      {!tasks && <Skeleton label={t('Loading automations')} count={5} height="52px" />}

      {tasks && tasks.length === 0 && (
        <EmptyState
          icon={Workflow}
          title={t('No automations yet')}
          body={t('An automation is a recipe: when it fires, what it does and where it delivers. Creating the first one is still done in the previous interface for now.')}
          primaryAction={{
            label: t('Open the previous interface'),
            onClick: () => {
              window.location.href = '/?shell=legacy';
            },
          }}
        />
      )}

      {tasks && tasks.length > 0 && (
        <div className="fs-list fs-list--rail">
          {tasks.map((task) => (
            <article
              className="fs-auto"
              key={task.id}
              data-state={task.status === 'active' ? 'succeeded' : 'paused'}
              data-testid="automation-row"
            >
              <span className="fs-auto__main">
                <span className="fs-row__name">{task.name}</span>
                <span className="fs-auto__recipe">
                  {describeTrigger(task)} → {describeAction(task)}
                </span>
                <span className="fs-row__meta">
                  {[
                    task.last_run ? `${t('last')} ${relativeTime(task.last_run)}` : t('has never run'),
                    task.run_count ? tn(task.run_count, '{n} run', '{n} runs') : null,
                    task.output_target && task.output_target !== 'none'
                      ? `entrega: ${task.output_target}`
                      : null,
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </span>
              <span className="fs-auto__when">
                {/* A paused task keeps whatever next_run it had when it was
                    paused, and announcing a run that is not going to happen
                    is worse than saying nothing. */}
                {task.status === 'active' && task.next_run
                  ? `${t('next')} ${relativeTime(task.next_run)}`
                  : ''}
              </span>
              <StatusBadge
                status={task.status === 'active' ? 'succeeded' : 'paused'}
                label={task.status === 'active' ? t('Active') : t('Paused')}
              />
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
