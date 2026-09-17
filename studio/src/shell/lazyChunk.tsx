/* Route chunks after a rebuild.

   Every screen but Inicio is a hashed chunk fetched on first open. When the
   Studio is rebuilt while a window stays open (the desktop app, a tab left
   overnight), the page still holds yesterday's chunk names: the next screen
   the user opens 404s, `import()` rejects, and React unmounts the whole
   tree — a blank window with nothing to click (the desktop app on 17-09,
   Connectors). Two nets:

   1. `lazyChunk` reloads the page ONCE when a chunk fails to fetch — the
      new index.html carries the new names. A sessionStorage stamp keeps a
      genuinely missing chunk from reloading forever.
   2. `RouteErrorBoundary` keeps the shell (nav, title bar) on screen and
      shows the error with a Reload button for everything else. */
import { Component, lazy, type ComponentType, type ErrorInfo, type ReactNode } from 'react';
import { t } from '../i18n';

const STAMP = 'fs-chunk-reload';
const WINDOW_MS = 60_000;

export function isChunkLoadError(error: unknown): boolean {
  const msg = String((error as { message?: unknown })?.message ?? error ?? '');
  return (
    /Failed to fetch dynamically imported module/i.test(msg) ||
    /Importing a module script failed/i.test(msg) ||
    /error loading dynamically imported module/i.test(msg) ||
    /ChunkLoadError/i.test(msg) ||
    /Failed to load module script/i.test(msg)
  );
}

function reloadedRecently(): boolean {
  try {
    const at = Number(sessionStorage.getItem(STAMP) || 0);
    return Number.isFinite(at) && Date.now() - at < WINDOW_MS;
  } catch {
    return false;
  }
}

function stampReload(): void {
  try {
    sessionStorage.setItem(STAMP, String(Date.now()));
  } catch {
    /* private mode / blocked storage: reload anyway, once per page life */
  }
}

/** Reload once for a stale chunk; false when this page already did. */
export function reloadForStaleChunk(error: unknown): boolean {
  if (!isChunkLoadError(error) || reloadedRecently()) return false;
  stampReload();
  window.location.reload();
  return true;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function lazyChunk<T extends ComponentType<any>>(loader: () => Promise<{ default: T }>) {
  return lazy(() =>
    loader().catch((error: unknown) => {
      if (reloadForStaleChunk(error)) {
        // The page is going away; keep Suspense showing its skeleton meanwhile.
        return new Promise<{ default: T }>(() => {});
      }
      throw error;
    }),
  );
}

type BoundaryProps = { children: ReactNode };
type BoundaryState = { error: Error | null };

export class RouteErrorBoundary extends Component<BoundaryProps, BoundaryState> {
  state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Second net for a chunk that slipped past lazyChunk (a nested import()).
    if (reloadForStaleChunk(error)) return;
    console.error('[studio] screen crashed', error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    const stale = isChunkLoadError(error);
    return (
      <div className="fs-route-error" role="alert" data-testid="route-error">
        <h2>{stale ? t('This screen changed under you') : t('This screen crashed')}</h2>
        <p>
          {stale
            ? t('Faustus was updated while this window was open; reload to get the new version.')
            : String(error.message || error)}
        </p>
        <button type="button" className="fs-btn" onClick={() => window.location.reload()}>
          {t('Reload')}
        </button>
      </div>
    );
  }
}
