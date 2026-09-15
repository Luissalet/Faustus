import * as RadixPopover from '@radix-ui/react-popover';
import type { ReactNode } from 'react';

export interface PopoverProps {
  trigger: ReactNode;
  children: ReactNode;
  align?: 'start' | 'center' | 'end';
  side?: 'top' | 'right' | 'bottom' | 'left';
  testId?: string;
  /** Extra class on the content, for a surface that needs its own width. */
  className?: string;
  /** Uncontrolled by default (Radix owns open state); pass both to control
   *  it — e.g. to lazily load the popover's content only once it opens
   *  (git.ts's branch popover, which fetches branches on first open rather
   *  than for every repo row up front). */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** Chip menus on the composer: open upward, stay below the title bar
   *  and chat header, scroll inside one shared shell. */
  placement?: 'composer';
}

function composerCollisionPadding(): { top: number; right: number; bottom: number; left: number } {
  const edge = 12;
  let top = edge;
  if (typeof document !== 'undefined') {
    for (const sel of ['.fs-desktop-bar', '.fs-studio__head']) {
      const el = document.querySelector(sel);
      if (el instanceof HTMLElement) {
        top = Math.max(top, el.getBoundingClientRect().bottom + 8);
      }
    }
  }
  return { top: Math.round(top), right: edge, bottom: edge, left: edge };
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
  className,
  open,
  onOpenChange,
  placement,
}: PopoverProps) {
  const composer = placement === 'composer';
  const classes = ['fs-popover'];
  if (composer) classes.push('fs-studio__composer-menu');
  if (className) classes.push(className);
  return (
    <RadixPopover.Root open={open} onOpenChange={onOpenChange}>
      <RadixPopover.Trigger asChild>{trigger}</RadixPopover.Trigger>
      <RadixPopover.Portal container={document.getElementById('fs-overlay-root') ?? undefined}>
        <RadixPopover.Content
          className={classes.join(' ')}
          align={align}
          side={composer ? 'top' : side}
          sideOffset={6}
          collisionPadding={composer ? composerCollisionPadding() : 8}
          data-testid={testId}
        >
          {children}
        </RadixPopover.Content>
      </RadixPopover.Portal>
    </RadixPopover.Root>
  );
}
