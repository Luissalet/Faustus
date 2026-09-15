import type { Todo } from '../../adapters/chat';
import { t } from '../../i18n';

/**
 * The agent's current todowrite list, as a live rail. The Progress side
 * panel is the place this belongs: the transcript used to freeze the first
 * snapshot at the top of the message.
 */
export function ProgressList({ todos }: { todos: Todo[] }) {
  if (!todos.length) return null;
  return (
    <div className="fs-trace fs-studio__trace" data-testid="studio-progress" aria-live="polite" aria-label={t('Progress')}>
      {todos.map((step, i) => (
        <div
          key={`${step.content}-${i}`}
          className="fs-trace__step"
          data-state={step.status === 'completed' ? 'succeeded' : step.status === 'in_progress' ? 'running' : 'queued'}
        >
          <span className="fs-trace__node" aria-hidden="true" />
          <span className="fs-trace__label">{step.content}</span>
          {step.status === 'completed' && step.verified === false && <span className="fs-trace__meta">{t('no evidence')}</span>}
        </div>
      ))}
    </div>
  );
}
