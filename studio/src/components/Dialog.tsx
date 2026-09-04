import * as RadixDialog from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import type { ReactNode } from 'react';
import { IconButton } from './IconButton';

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children?: ReactNode;
  footer?: ReactNode;
  testId?: string;
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
}: DialogProps) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal container={document.getElementById('fs-overlay-root') ?? undefined}>
        <RadixDialog.Overlay className="fs-overlay-backdrop" />
        <RadixDialog.Content className="fs-dialog" data-testid={testId}>
          <div
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              justifyContent: 'space-between',
              gap: 'var(--fs-space-3)',
            }}
          >
            <RadixDialog.Title className="fs-dialog__title">{title}</RadixDialog.Title>
            <RadixDialog.Close asChild>
              <IconButton icon={X} label="Cerrar" size="sm" />
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
