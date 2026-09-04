/**
 * The application proper, loaded by main.tsx as ONE dynamic chunk.
 *
 * Why not just render from the entry: index.html loads the entry as
 * `studio.js?v=<hash>` for cache-busting, while every lazy chunk imports
 * what it shares with the entry by relative URL — `../studio.js`, no query.
 * The browser sees two different modules, so React, the router and the
 * stores exist twice; the first lazy dialog to open threw "invalid hook
 * call" and unmounted the shell. With the entry reduced to this import,
 * everything shared lives in hashed chunks that every importer names the
 * same way.
 */
import { StrictMode, Suspense, lazy } from 'react';
import { createRoot } from 'react-dom/client';
import { AppShell } from './shell/AppShell';

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

export function mount(gallery: boolean): void {
  createRoot(mountPoint()).render(
    <StrictMode>
      {gallery ? (
        <Suspense fallback={null}>
          <Gallery />
        </Suspense>
      ) : (
        <AppShell />
      )}
    </StrictMode>,
  );
}
