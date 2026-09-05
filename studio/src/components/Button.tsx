import { Loader2, type LucideIcon } from 'lucide-react';
import { slug } from './testid';

export interface ButtonProps {
  /**
   * `danger` is an outline: destructive actions must not compete with the
   * brand's primary fill. `danger-solid` is only for the confirming button
   * inside a destructive dialog, where it is that dialog's main action.
   */
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger' | 'danger-solid';
  size?: 'sm' | 'md' | 'lg';
  label: string;
  icon?: LucideIcon;
  iconPosition?: 'left' | 'right';
  loading?: boolean;
  disabled?: boolean;
  type?: 'button' | 'submit';
  testId?: string;
  /** A native tooltip, for the buttons whose label is shorter than the promise. */
  title?: string;
  onClick?: () => void;
}

/**
 * The primary action control. Always a real <button>: a div with onClick
 * is a lint failure in this tree, because it costs keyboard users the
 * control entirely.
 */
export function Button({
  variant = 'secondary',
  size = 'md',
  label,
  icon: Icon,
  iconPosition = 'left',
  loading = false,
  disabled = false,
  type = 'button',
  testId,
  title,
  onClick,
}: ButtonProps) {
  const iconSize = size === 'lg' ? 18 : 16;
  const glyph = loading ? (
    <Loader2 className="fs-btn__spinner" size={iconSize} aria-hidden="true" />
  ) : Icon ? (
    <Icon size={iconSize} aria-hidden="true" />
  ) : null;

  return (
    <button
      type={type}
      className="fs-btn"
      data-variant={variant}
      data-size={size}
      data-testid={testId ?? `btn-${slug(label)}`}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      title={title}
      onClick={onClick}
    >
      {iconPosition === 'left' && glyph}
      <span>{label}</span>
      {iconPosition === 'right' && glyph}
    </button>
  );
}
