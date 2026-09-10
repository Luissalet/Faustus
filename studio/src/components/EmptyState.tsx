import type { LucideIcon } from 'lucide-react';
import { Lock, RefreshCw } from 'lucide-react';
import { Button, type ButtonProps } from './Button';

/**
 * ACT-06: a screen has four states, not two — loading, empty, error (with
 * an action), and states neither of those names fits: no permission to see
 * this, or the data on screen is from a version the server no longer has
 * (a stale approval, a re-planned run). `tone` names those last two
 * explicitly instead of every screen reaching for `tone="error"` and a
 * hand-written icon for what is really a different situation — a denied
 * screen is not broken, and retrying an incompatible-version screen does
 * not fix it the way retrying a network error does.
 */
export type EmptyStateTone = 'empty' | 'error' | 'denied' | 'incompatible';

const TONE_ICON: Partial<Record<EmptyStateTone, LucideIcon>> = {
  denied: Lock,
  incompatible: RefreshCw,
};

export interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  body: string;
  /** An empty state without a way out is just a dead end with better typography. */
  primaryAction?: Pick<ButtonProps, 'label' | 'icon' | 'onClick'>;
  secondaryAction?: Pick<ButtonProps, 'label' | 'icon' | 'onClick'>;
  headingLevel?: 2 | 3;
  /** Defaults to `'empty'`: existing callers (a plain "nothing here yet")
   *  are unaffected. `'error'`/`'denied'` announce immediately (`role="alert"`)
   *  since they need the person's attention now; `'empty'`/`'incompatible'`
   *  do not interrupt. */
  tone?: EmptyStateTone;
}

export function EmptyState({
  icon: Icon,
  title,
  body,
  primaryAction,
  secondaryAction,
  headingLevel = 2,
  tone = 'empty',
}: EmptyStateProps) {
  const Heading = `h${headingLevel}` as 'h2' | 'h3';
  const ToneIcon = Icon ?? TONE_ICON[tone];

  return (
    <div
      className="fs-empty"
      data-testid="empty-state"
      data-tone={tone}
      role={tone === 'error' || tone === 'denied' ? 'alert' : undefined}
    >
      {ToneIcon && <ToneIcon className="fs-empty__icon" size={24} aria-hidden="true" />}
      <Heading className="fs-empty__title">{title}</Heading>
      <p className="fs-empty__body">{body}</p>
      {(primaryAction || secondaryAction) && (
        <div className="fs-empty__actions">
          {primaryAction && <Button variant="primary" {...primaryAction} />}
          {secondaryAction && <Button variant="ghost" {...secondaryAction} />}
        </div>
      )}
    </div>
  );
}
