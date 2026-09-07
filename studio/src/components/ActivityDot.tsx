import type { SessionActivity } from '../shell/activity';
import type { RunActivityDetail } from '../adapters/chat';
import { t } from '../i18n';

export interface ActivityDotProps {
  state: SessionActivity;
  /** Position in the queue, when the state is `queued`. */
  position?: number;
  /** Puts the state in words next to the dot (roomy rows). */
  withLabel?: boolean;
  /** Server-side phase; lets a live dot say more than merely "running". */
  detail?: RunActivityDetail;
}

const LABEL: Record<SessionActivity, string> = {
  running: 'Working now',
  waiting: 'Waiting for your permission',
  queued: 'Waiting for its turn',
};

/**
 * The dot that says a conversation is alive.
 *
 * Colour is never the only signal: the label is always in the markup (read
 * aloud by a screen reader, shown on hover), and the three states differ in
 * movement as well as in colour — the amber one that needs a person does not
 * breathe, because a decision is not progress.
 */
export function ActivityDot({ state, position, withLabel = false, detail }: ActivityDotProps) {
  let label = state === 'queued' && position
    ? t('Waiting for its turn (#{n})', { n: position })
    : t(LABEL[state]);
  if (state === 'running' && detail) {
    if (detail.phase === 'tool' && detail.tool) {
      label = t('Using {tool}', { tool: detail.tool.replace(/_/g, ' ') });
      if (detail.detail) label += ` · ${detail.detail.slice(0, 80)}`;
    } else if (detail.phase === 'thinking') label = t('Thinking');
    else if (detail.phase === 'writing') label = t('Writing');
    else if (detail.phase === 'research') label = detail.detail || t('Researching');
    else if (detail.phase === 'waiting_model') label = t('Waiting for the model');
    else if (detail.phase === 'starting') label = t('Starting');
  }
  return (
    <span className="fs-live" data-state={state} title={label} data-testid={`activity-${state}`}>
      <span className="fs-live__dot" aria-hidden="true" />
      <span className="fs-live__label" data-quiet={withLabel ? undefined : ''}>
        {label}
      </span>
    </span>
  );
}
