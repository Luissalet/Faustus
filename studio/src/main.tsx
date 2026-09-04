/**
 * Faustus Studio — entry point.
 *
 * This file exists only to prove the toolchain compiles. The actual
 * shell is UI-021; nothing mounts into the live app yet.
 */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

function StudioBootstrap() {
  return null;
}

const root = document.getElementById('studio-root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <StudioBootstrap />
    </StrictMode>,
  );
}
