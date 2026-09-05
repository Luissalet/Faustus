import { t } from '../i18n';
import { ChevronDown, Cpu } from 'lucide-react';
import { lazy, Suspense, useEffect, useState } from 'react';
import type { ModelRoute } from '../adapters/chat';

const ModelPalette = lazy(() => import('./ModelPalette'));

/**
 * The model picker is a palette, not a dropdown.
 *
 * Two reasons. A box with forty models in it needs a search field, and
 * cmdk is the palette Ctrl+K already uses; the Radix dropdown would have
 * pulled floating-ui in for the first time — 80 KB, a quarter of the
 * budget — to draw a list the palette draws better. The palette itself is
 * a lazy chunk: the chip is on every page load, the list only when opened.
 */
export function ModelPicker({
  routes,
  current,
  onPick,
  onRefresh,
  refreshing,
  openSignal = 0,
}: {
  routes: ModelRoute[];
  current: ModelRoute | null;
  onPick: (route: ModelRoute) => void;
  onRefresh?: () => void;
  refreshing?: boolean;
  /** Bump to open the palette from outside (the /models command). */
  openSignal?: number;
}) {
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (openSignal > 0) {
      setLoaded(true);
      setOpen(true);
    }
  }, [openSignal]);

  return (
    <>
      <button
        type="button"
        className="fs-studio__chip fs-studio__chip--model"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => {
          setLoaded(true);
          setOpen(true);
        }}
        data-testid="studio-model"
      >
        <Cpu size={13} aria-hidden="true" />
        <span>{current ? current.model : routes.length ? t('Choose model') : t('No models')}</span>
        <ChevronDown size={12} aria-hidden="true" />
      </button>
      {loaded && (
        <Suspense fallback={null}>
          <ModelPalette open={open} onOpenChange={setOpen} routes={routes} current={current} onPick={onPick} onRefresh={onRefresh} refreshing={refreshing} />
        </Suspense>
      )}
    </>
  );
}
