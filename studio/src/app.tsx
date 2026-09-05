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

/*
 * Mount exactly once, whatever the entry does.
 *
 * index.html loads the entry as `studio.js?v=<hash>`; this chunk imports
 * Vite's preload helper from `../studio.js` (no query), so the browser
 * evaluates main.tsx a second time and its `.then(mount)` fires twice.
 * Two roots on one container: the second commit clears the container, the
 * first root keeps rendering into detached nodes, and the next time it
 * removes one — re-keying the tree on a language change did it — React
 * throws "removeChild: not a child" and unmounts the whole shell. The guard
 * lives here, in the one module that exists once, not in main.tsx, which
 * is the module that exists twice.
 */
let mounted = false;

export function mount(gallery: boolean): void {
  if (mounted) return;
  mounted = true;
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
