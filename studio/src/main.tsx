/**
 * Faustus Studio — entry point.
 *
 * Two mounts, both explicit:
 *
 *   ?gallery=1                     → the component gallery (UI-011 review)
 *   flag `faustus_studio_shell`    → the AppShell (UI-021)
 *
 * With neither, this file does nothing at all. That is deliberate: the
 * bundle can be loaded on the legacy page without touching it, and the
 * rollback is a reload with the flag off.
 *
 * The gallery is lazy on purpose. It is a review surface that no shipped
 * screen imports, and letting it ride in the entry bundle pushed the build
 * past its budget — 120.45 KB gzip against a 120 KB ceiling. A budget you
 * quietly exceed is not a budget.
 */

import { StrictMode, Suspense, lazy } from 'react';
import { createRoot } from 'react-dom/client';
import './styles/fonts.css';
import './styles/tokens.css';
import './styles/base.css';
import './styles/legacy-bridge.css';
import './styles/components.css';
import './styles/shell.css';
import { AppShell } from './shell/AppShell';
import { isStudioEnabled } from './shell/flag';

const Gallery = lazy(async () => ({
  default: (await import('./gallery/Gallery')).Gallery,
}));

function mountPoint(): HTMLElement {
  const existing = document.getElementById('studio-root');
  if (existing) return existing;
  const created = document.createElement('div');
  created.id = 'studio-root';
  document.body.appendChild(created);
  return created;
}

const wantsGallery = new URLSearchParams(window.location.search).has('gallery');

if (wantsGallery) {
  createRoot(mountPoint()).render(
    <StrictMode>
      <Suspense fallback={null}>
        <Gallery />
      </Suspense>
    </StrictMode>,
  );
} else if (isStudioEnabled()) {
  createRoot(mountPoint()).render(
    <StrictMode>
      <AppShell />
    </StrictMode>,
  );
}
