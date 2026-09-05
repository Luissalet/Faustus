import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { ensureOverlayRoot } from '../shell/overlayRoot';

/**
 * A short status line at the bottom of the viewport.
 *
 * Portalled into the overlay root on purpose: every screen's children carry
 * the entrance animation (a transform), which turns them into the containing
 * block of any `position: fixed` inside them and paints them under a Radix
 * dialog's overlay. Out here it is really fixed and really on top.
 */
export function Toast({ children, testId = 'toast' }: { children: ReactNode; testId?: string }) {
  return createPortal(
    <div className="fs-toast" role="status" data-testid={testId}>
      {children}
    </div>,
    ensureOverlayRoot(),
  );
}
