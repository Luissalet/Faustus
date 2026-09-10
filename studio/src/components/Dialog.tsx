import * as RadixDialog from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import type { ReactNode } from 'react';
import { IconButton } from './IconButton';
import { t } from '../i18n';

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children?: ReactNode;
  footer?: ReactNode;
  testId?: string;
  className?: string;
}

/**
 * Radix owns the hard parts — focus trap, Escape, inert background,
 * aria-modal, scroll lock — and we own the appearance. Reimplementing
 * any of that by hand is how dialogs end up unusable by keyboard.
 */
export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  testId = 'dialog',
  className = '',
}: DialogProps) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal container={document.getElementById('fs-overlay-root') ?? undefined}>
        <RadixDialog.Overlay className="fs-overlay-backdrop" />
        <RadixDialog.Content className={`fs-dialog ${className}`} data-testid={testId}>
          <div
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              justifyContent: 'space-between',
              gap: 'var(--fs-space-3)',
              // A11Y-01/QA-44: at 200% zoom the dialog's own width shrinks
              // (`.fs-dialog`'s `inline-size: min(560px, calc(100vw - ...))`)
              // but a flex item's default `min-inline-size: auto` still lets
              // a long, unbroken title (a filename, an id) push this row —
              // and the Close button riding along in it — past the dialog's
              // right edge and off the viewport (scripts/ui_a11y.py's
              // "in viewport" check). Bounding the row and letting the title
              // shrink/wrap keeps the Close button reachable regardless of
              // zoom or title length.
              maxInlineSize: '100%',
            }}
          >
            <RadixDialog.Title
              className="fs-dialog__title"
              style={{ minInlineSize: 0, maxInlineSize: '100%', overflowWrap: 'anywhere' }}
            >
              {title}
            </RadixDialog.Title>
            <RadixDialog.Close asChild>
              <IconButton icon={X} label={t('Close')} size="sm" />
            </RadixDialog.Close>
          </div>
          {description && (
            <RadixDialog.Description className="fs-dialog__body">
              {description}
            </RadixDialog.Description>
          )}
          {children}
          {footer && <div className="fs-dialog__actions">{footer}</div>}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
