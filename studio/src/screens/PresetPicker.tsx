import { Sparkles, X } from 'lucide-react';
import { Suspense, useEffect, useRef, useState } from 'react';
import { lazyChunk } from '../shell/lazyChunk';
import type { Preset } from '../adapters/presets';
import { t } from '../i18n';

const PresetPalette = lazyChunk(() => import('./PresetPalette'));

/**
 * The preset chip: a name when one is on, a palette when clicked.
 *
 * A preset swaps the system prompt of EVERY new chat (it is kept globally),
 * so an active one must never hide behind a menu: with `hideWhenEmpty` the
 * picker lives in the composer bar and shows its chip only while a preset is
 * on; with none it renders just the palette, opened by `openSignal`.
 */
export function PresetPicker({
  current,
  onPick,
  onNotice,
  openSignal = 0,
  hideWhenEmpty = false,
}: {
  current: { id: string; name: string } | null;
  onPick: (preset: Preset | null) => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
  /** Bump to open the palette from outside (the /preset command). */
  openSignal?: number;
  /** No chip while no preset is on (the bar instance). */
  hideWhenEmpty?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);
  // Only a bump AFTER mount opens the palette: a remount (new session, the
  // composer re-rendering) with an old signal must not pop it open again.
  const seenSignal = useRef(openSignal);
  useEffect(() => {
    if (openSignal > 0 && openSignal !== seenSignal.current) {
      seenSignal.current = openSignal;
      setLoaded(true);
      setOpen(true);
    }
  }, [openSignal]);
  const palette = loaded ? (
    <Suspense fallback={null}>
      <PresetPalette open={open} onOpenChange={setOpen} current={current?.id ?? null} onPick={onPick} onNotice={onNotice} />
    </Suspense>
  ) : null;
  if (!current && hideWhenEmpty) return palette;
  return (
    <span className="fs-studio__chipgroup">
      <button
        type="button"
        className="fs-studio__chip"
        aria-pressed={Boolean(current)}
        aria-haspopup="dialog"
        aria-expanded={open}
        title={current ? `Preset: ${current.name}` : t('Preset or persona (system prompt)')}
        onClick={() => {
          setLoaded(true);
          setOpen(true);
        }}
        data-testid="studio-preset"
      >
        <Sparkles size={13} aria-hidden="true" />
        <span>{current ? current.name : t('Preset')}</span>
      </button>
      {current && (
        <button type="button" className="fs-studio__chip-x" aria-label={t('Remove the preset')} onClick={() => onPick(null)}>
          <X size={11} aria-hidden="true" />
        </button>
      )}
      {palette}
    </span>
  );
}
