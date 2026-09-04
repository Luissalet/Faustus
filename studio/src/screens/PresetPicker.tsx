import { Sparkles, X } from 'lucide-react';
import { lazy, Suspense, useEffect, useState } from 'react';
import type { Preset } from '../adapters/presets';

const PresetPalette = lazy(() => import('./PresetPalette'));

/** The preset chip: a name when one is on, a palette when clicked. */
export function PresetPicker({
  current,
  onPick,
  onNotice,
  openSignal = 0,
}: {
  current: { id: string; name: string } | null;
  onPick: (preset: Preset | null) => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
  /** Bump to open the palette from outside (the /preset command). */
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
    <span className="fs-studio__chipgroup">
      <button
        type="button"
        className="fs-studio__chip"
        aria-pressed={Boolean(current)}
        aria-haspopup="dialog"
        aria-expanded={open}
        title={current ? `Preset: ${current.name}` : 'Preset o personaje (prompt de sistema)'}
        onClick={() => {
          setLoaded(true);
          setOpen(true);
        }}
        data-testid="studio-preset"
      >
        <Sparkles size={13} aria-hidden="true" />
        <span>{current ? current.name : 'Preset'}</span>
      </button>
      {current && (
        <button type="button" className="fs-studio__chip-x" aria-label="Quitar el preset" onClick={() => onPick(null)}>
          <X size={11} aria-hidden="true" />
        </button>
      )}
      {loaded && (
        <Suspense fallback={null}>
          <PresetPalette open={open} onOpenChange={setOpen} current={current?.id ?? null} onPick={onPick} onNotice={onNotice} />
        </Suspense>
      )}
    </span>
  );
}
