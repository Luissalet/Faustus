import type { SessionActivity } from '../shell/activity';
import { t } from '../i18n';

export interface ActivityDotProps {
  state: SessionActivity;
  /** Position in the queue, when the state is `queued`. */
  position?: number;
  /** Puts the state in words next to the dot (roomy rows). */
  withLabel?: boolean;
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
export function ActivityDot({ state, position, withLabel = false }: ActivityDotProps) {
  const label =
    state === 'queued' && position ? t('Waiting for its turn (#{n})', { n: position }) : t(LABEL[state]);
  return (
    <span className="fs-live" data-state={state} title={label} data-testid={`activity-${state}`}>
      <span className="fs-live__dot" aria-hidden="true" />
      <span className="fs-live__label" data-quiet={withLabel ? undefined : ''}>
        {label}
      </span>
    </span>
  );
}
