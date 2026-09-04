import type { LucideIcon } from 'lucide-react';
import { Button, type ButtonProps } from './Button';

export interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  body: string;
  /** An empty state without a way out is just a dead end with better typography. */
  primaryAction?: Pick<ButtonProps, 'label' | 'icon' | 'onClick'>;
  secondaryAction?: Pick<ButtonProps, 'label' | 'icon' | 'onClick'>;
  headingLevel?: 2 | 3;
}

export function EmptyState({
  icon: Icon,
  title,
  body,
  primaryAction,
  secondaryAction,
  headingLevel = 2,
}: EmptyStateProps) {
  const Heading = `h${headingLevel}` as 'h2' | 'h3';

  return (
    <div className="fs-empty" data-testid="empty-state">
      {Icon && <Icon className="fs-empty__icon" size={24} aria-hidden="true" />}
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
