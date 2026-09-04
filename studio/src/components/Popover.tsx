import * as RadixPopover from '@radix-ui/react-popover';
import type { ReactNode } from 'react';

export interface PopoverProps {
  trigger: ReactNode;
  children: ReactNode;
  align?: 'start' | 'center' | 'end';
  side?: 'top' | 'right' | 'bottom' | 'left';
  testId?: string;
}

/**
 * For content that needs focus — a small form, a filter, an explanation
 * with a link. Anything that is only a label belongs in a tooltip, and
 * anything critical belongs on the page: hover is not a place to keep
 * information (DESIGN.md, "no esconder información crítica en hover").
 */
export function Popover({
  trigger,
  children,
  align = 'start',
  side = 'bottom',
  testId = 'popover',
}: PopoverProps) {
  return (
    <RadixPopover.Root>
      <RadixPopover.Trigger asChild>{trigger}</RadixPopover.Trigger>
      <RadixPopover.Portal>
        <RadixPopover.Content
          className="fs-popover"
          align={align}
          side={side}
          sideOffset={6}
          data-testid={testId}
        >
          {children}
        </RadixPopover.Content>
      </RadixPopover.Portal>
    </RadixPopover.Root>
  );
}
