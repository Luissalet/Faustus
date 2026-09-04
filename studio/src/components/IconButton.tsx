import type { LucideIcon } from 'lucide-react';
import { slug } from './testid';

export interface IconButtonProps {
  icon: LucideIcon;
  /** Required. An icon-only control with no accessible name is unusable. */
  label: string;
  variant?: 'ghost' | 'secondary';
  size?: 'sm' | 'md';
  badge?: number | boolean;
  disabled?: boolean;
  testId?: string;
  onClick?: () => void;
}

export function IconButton({
  icon: Icon,
  label,
  variant = 'ghost',
  size = 'md',
  badge,
  disabled = false,
  testId,
  onClick,
}: IconButtonProps) {
  const isDot = badge === true;
  const showBadge = badge === true || (typeof badge === 'number' && badge > 0);

  return (
    <button
      type="button"
      className="fs-icon-btn"
      data-variant={variant}
      data-size={size}
      data-testid={testId ?? `icon-btn-${slug(label)}`}
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
    >
      <Icon size={size === 'sm' ? 15 : 17} aria-hidden="true" />
      {showBadge && (
        <span className="fs-icon-btn__badge" data-dot={isDot} aria-hidden="true">
          {isDot ? '' : badge}
        </span>
      )}
    </button>
  );
}
