import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Dialog, Skeleton } from '../../../components';
import { recentImages, type LibraryPick } from '../../../adapters/imageTools';
import { adjustmentLabel, defaultParams, drawHistogram, luminanceHistogram, type Adjustment, type AdjustmentType, type Rgb } from '../../../lib/pixel';
import { t } from '../../../i18n';
import { Segmented, Slider } from './controls';
import type { PixelEditor } from './engine';

/* ── Canvas size (also the "new canvas" dialog) ── */

export const CANVAS_PRESETS: { label: string; w: number; h: number }[] = [
  { label: 'Square 1024', w: 1024, h: 1024 },
  { label: 'Landscape 1536 × 1024', w: 1536, h: 1024 },
  { label: 'Portrait 1024 × 1536', w: 1024, h: 1536 },
  { label: 'HD 1920 × 1080', w: 1920, h: 1080 },
  { label: 'Story 1080 × 1920', w: 1080, h: 1920 },
  { label: 'Print A4 300 dpi', w: 2480, h: 3508 },
];

export function CanvasSizeDialog({ open, onOpenChange, width, height, mode, onApply }: { open: boolean; onOpenChange: (o: boolean) => void; width: number; height: number; mode: 'new' | 'resize'; onApply: (w: number, h: number, anchor: 'center' | 'tl') => void }) {
  const [w, setW] = useState(width);
  const [h, setH] = useState(height);
  const [anchor, setAnchor] = useState<'center' | 'tl'>('center');
  useEffect(() => {
    if (open) {
      setW(width);
      setH(height);
    }
  }, [open, width, height]);
  const valid = w >= 1 && h >= 1 && w <= 16384 && h <= 16384;
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={mode === 'new' ? t('New canvas') : t('Canvas size')}
      description={mode === 'new' ? t('Start from a blank, white canvas.') : t('Change the canvas bounds. Layers keep their pixels; what falls outside is still there, just not visible.')}
      testId="canvas-size"
      footer={
        <>
          <Button variant="ghost" label={t('Cancel')} onClick={() => onOpenChange(false)} />
          <Button variant="primary" label={mode === 'new' ? t('Create') : t('Resize')} disabled={!valid} onClick={() => onApply(Math.round(w), Math.round(h), anchor)} />
        </>
      }
    >
      <div className="fs-ed__presets">
        {CANVAS_PRESETS.map((p) => (
          <button key={p.label} type="button" className="fs-chip" data-on={p.w === w && p.h === h ? '' : undefined} onClick={() => { setW(p.w); setH(p.h); }}>
            {t(p.label)}
          </button>
        ))}
      </div>
      <div className="fs-ed__row">
        <div className="fs-ed__field">
          <label htmlFor="ed-cs-w">{t('Width')}</label>
          <input id="ed-cs-w" className="fs-field" type="number" min={1} max={16384} value={w} onChange={(e) => setW(Number(e.target.value))} />
        </div>
        <div className="fs-ed__field">
          <label htmlFor="ed-cs-h">{t('Height')}</label>
          <input id="ed-cs-h" className="fs-field" type="number" min={1} max={16384} value={h} onChange={(e) => setH(Number(e.target.value))} />
        </div>
      </div>
      {mode === 'resize' && <Segmented label={t('Anchor')} value={anchor} options={[{ value: 'center', label: t('Keep centred') }, { value: 'tl', label: t('Keep top-left') }]} onChange={setAnchor} />}
    </Dialog>
  );
}

/** While a preview dialog is open, the scrim goes so the picture stays visible. */
function usePreviewMode(open: boolean): void {
  useEffect(() => {
    if (!open) return;
    document.documentElement.setAttribute('data-fs-preview', '');
    return () => document.documentElement.removeAttribute('data-fs-preview');
  }, [open]);
}

/* ── Blur filters, previewed live on the active layer ── */

export type BlurKind = 'gaussian' | 'zoom' | 'motion';

export function BlurDialog({ ed, kind, onClose }: { ed: PixelEditor; kind: BlurKind | null; onClose: () => void }) {
  const [radius, setRadius] = useState(6);
  const [strength, setStrength] = useState(30);
  const [angle, setAngle] = useState(0);
  const [length, setLength] = useState(20);
  usePreviewMode(!!kind);
  useEffect(() => {
    if (!kind) return;
    ed.previewFilter(kind, { radius, strength, angle, length });
  }, [ed, kind, radius, strength, angle, length]);
  const title = kind === 'gaussian' ? t('Gaussian blur') : kind === 'zoom' ? t('Zoom blur') : t('Motion blur');
  return (
    <Dialog
      open={!!kind}
      onOpenChange={(o) => {
        if (!o) {
          ed.cancelFilter();
          onClose();
        }
      }}
      title={title}
      description={t('Applies to the active layer. You are looking at the result.')}
      testId="blur"
      footer={
        <>
          <Button variant="ghost" label={t('Cancel')} onClick={() => { ed.cancelFilter(); onClose(); }} />
          <Button variant="primary" label={t('Apply')} onClick={() => { ed.commitFilter(title); onClose(); }} />
        </>
      }
    >
      {kind === 'gaussian' && <Slider id="ed-blur-r" label={t('Radius')} value={radius} min={0} max={60} onChange={setRadius} format={(v) => `${v} px`} />}
      {kind === 'zoom' && <Slider id="ed-blur-s" label={t('Strength')} value={strength} min={0} max={100} onChange={setStrength} format={(v) => `${v}%`} />}
      {kind === 'motion' && (
        <>
          <Slider id="ed-blur-a" label={t('Angle')} value={angle} min={-180} max={180} onChange={setAngle} format={(v) => `${v}°`} />
          <Slider id="ed-blur-l" label={t('Length')} value={length} min={2} max={80} onChange={setLength} format={(v) => `${v} px`} />
        </>
      )}
    </Dialog>
  );
}

/* ── Adjustment layers ── */

export interface AdjustTarget {
  layerId: string;
  type: AdjustmentType;
  adjId?: string;
}

export function AdjustDialog({ ed, target, onClose }: { ed: PixelEditor; target: AdjustTarget | null; onClose: () => void }) {
  const layer = target ? ed.doc.layers.find((l) => l.id === target.layerId) : null;
  const existing = target?.adjId ? layer?.adjustments.find((a) => a.id === target.adjId) : null;
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [zone, setZone] = useState<'shadows' | 'midtones' | 'highlights'>('midtones');
  const histRef = useRef<HTMLCanvasElement>(null);
  const type = target?.type ?? 'brightness-contrast';
  usePreviewMode(!!target);

  useEffect(() => {
    if (!target) return;
    setParams(existing ? JSON.parse(JSON.stringify(existing.params)) : defaultParams(target.type));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.layerId, target?.type, target?.adjId]);

  useEffect(() => {
    if (!target || !layer) return;
    const adj: Adjustment = { type: target.type, params };
    ed.stageAdjustment(target.layerId, adj, target.adjId ?? null);
    return () => ed.stageAdjustment(target.layerId, null);
  }, [ed, target, layer, params]);

  const hist = useMemo(() => (layer && type === 'levels' ? luminanceHistogram(layer.canvas) : null), [layer, type]);
  useEffect(() => {
    const c = histRef.current;
    if (!c || !hist) return;
    const css = getComputedStyle(c);
    const p = params as { inBlack: number; inWhite: number };
    drawHistogram(c, hist, { bar: css.getPropertyValue('--fs-text-3').trim() || css.color, black: css.getPropertyValue('--fs-text-1').trim() || css.color, white: css.getPropertyValue('--fs-brand').trim() || css.color }, { inBlack: p.inBlack ?? 0, inWhite: p.inWhite ?? 255 });
  }, [hist, params]);

  if (!target || !layer) return null;
  const p = params as Record<string, number>;
  const set = (k: string, v: number) => setParams((cur) => ({ ...cur, [k]: v }));
  const cb = params as { shadows: Rgb; midtones: Rgb; highlights: Rgb };
  const setCb = (ch: keyof Rgb, v: number) => setParams((cur) => ({ ...cur, [zone]: { ...(cur[zone] as Rgb), [ch]: v } }));
  const title = t(adjustmentLabel(type));

  return (
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      title={existing ? t('Edit {name}', { name: existing.name }) : title}
      description={t('An adjustment layer on {layer}: the pixels stay untouched and you can change or remove it later.', { layer: layer.name })}
      testId="adjust"
      footer={
        <>
          <Button variant="ghost" label={t('Cancel')} onClick={onClose} />
          <Button
            variant="primary"
            label={existing ? t('Save') : t('Add adjustment')}
            onClick={() => {
              if (existing) ed.updateAdjustment(layer.id, existing.id, { params: JSON.parse(JSON.stringify(params)) }, true);
              else ed.addAdjustment(layer.id, { type, params: JSON.parse(JSON.stringify(params)) }, title);
              onClose();
            }}
          />
        </>
      }
    >
      {type === 'brightness-contrast' && (
        <>
          <Slider id="adj-b" label={t('Brightness')} value={Math.round(((p.brightness ?? 1) - 1) * 100)} min={-100} max={100} onChange={(v) => set('brightness', 1 + v / 100)} />
          <Slider id="adj-c" label={t('Contrast')} value={Math.round(((p.contrast ?? 1) - 1) * 100)} min={-100} max={100} onChange={(v) => set('contrast', 1 + v / 100)} />
        </>
      )}
      {type === 'hue-saturation' && (
        <>
          <Slider id="adj-h" label={t('Hue')} value={Math.round(p.hue ?? 0)} min={-180} max={180} onChange={(v) => set('hue', v)} format={(v) => `${v}°`} />
          <Slider id="adj-s" label={t('Saturation')} value={Math.round(((p.saturation ?? 1) - 1) * 100)} min={-100} max={100} onChange={(v) => set('saturation', 1 + v / 100)} />
        </>
      )}
      {type === 'levels' && (
        <>
          <canvas ref={histRef} className="fs-ed__hist" width={256} height={72} aria-label={t('Luminance histogram')} role="img" />
          <Slider id="adj-ib" label={t('Input black')} value={p.inBlack ?? 0} min={0} max={254} onChange={(v) => set('inBlack', v)} />
          <Slider id="adj-iw" label={t('Input white')} value={p.inWhite ?? 255} min={1} max={255} onChange={(v) => set('inWhite', v)} />
          <Slider id="adj-g" label={t('Gamma')} value={Math.round((p.gamma ?? 1) * 100)} min={10} max={300} onChange={(v) => set('gamma', v / 100)} format={(v) => (v / 100).toFixed(2)} />
          <Slider id="adj-ob" label={t('Output black')} value={p.outBlack ?? 0} min={0} max={255} onChange={(v) => set('outBlack', v)} />
          <Slider id="adj-ow" label={t('Output white')} value={p.outWhite ?? 255} min={0} max={255} onChange={(v) => set('outWhite', v)} />
        </>
      )}
      {type === 'color-balance' && cb.shadows && (
        <>
          <Segmented label={t('Tones')} value={zone} options={[{ value: 'shadows', label: t('Shadows') }, { value: 'midtones', label: t('Midtones') }, { value: 'highlights', label: t('Highlights') }]} onChange={setZone} />
          <Slider id="adj-r" label={t('Cyan – Red')} value={cb[zone].r} min={-100} max={100} onChange={(v) => setCb('r', v)} />
          <Slider id="adj-gg" label={t('Magenta – Green')} value={cb[zone].g} min={-100} max={100} onChange={(v) => setCb('g', v)} />
          <Slider id="adj-bb" label={t('Yellow – Blue')} value={cb[zone].b} min={-100} max={100} onChange={(v) => setCb('b', v)} />
        </>
      )}
    </Dialog>
  );
}

/* ── Import from the library ── */

export function LibraryPickDialog({ open, onOpenChange, onPick }: { open: boolean; onOpenChange: (o: boolean) => void; onPick: (pick: LibraryPick) => void }) {
  const [items, setItems] = useState<LibraryPick[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setItems(null);
    recentImages(60)
      .then(setItems)
      .catch((e: Error) => setError(e.message));
  }, [open]);
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t('Import from the library')} description={t('The picture lands on its own layer; drag it into place with Move.')} testId="library-pick">
      {error && (
        <p className="fs-notice" data-tone="warning">
          {error}
        </p>
      )}
      {!items && !error && <Skeleton label={t('Loading images')} count={3} />}
      {items && (
        <div className="fs-ed__picks">
          {items.map((it) => (
            <button key={it.id} type="button" className="fs-ed__pick" onClick={() => onPick(it)} title={it.name}>
              <img src={it.url} alt={it.name} loading="lazy" />
            </button>
          ))}
          {!items.length && <p className="fs-ed__help">{t('The library has no images yet.')}</p>}
        </div>
      )}
    </Dialog>
  );
}

/* ── Keyboard shortcuts ── */

const SHORTCUTS: [string, string][] = [
  ['V', 'Move'],
  ['C', 'Crop'],
  ['T', 'Transform'],
  ['B', 'Brush'],
  ['E', 'Eraser'],
  ['K', 'Clone stamp (Alt-click sets the source)'],
  ['L', 'Lasso'],
  ['W', 'Magic wand'],
  ['M', 'Inpaint'],
  ['S', 'Sharpen'],
  ['[ / ]', 'Brush size − / +'],
  ['Space + drag', 'Pan'],
  ['Wheel', 'Zoom'],
  ['Ctrl+Z / Ctrl+Shift+Z', 'Undo / Redo'],
  ['Ctrl+S', 'Save over the original'],
  ['Ctrl+Shift+S', 'Save as a copy'],
  ['Ctrl+Alt+J', 'New layer'],
  ['Ctrl+Alt+T', 'Free transform'],
  ['Ctrl+Shift+T', 'Canvas size'],
  ['Ctrl+A', 'Select all'],
  ['Ctrl+Shift+D', 'Deselect'],
  ['Ctrl+Alt+I', 'Invert selection'],
  ['Ctrl+C', 'Copy the selection to a layer'],
  ['Ctrl+X', 'Cut the selection to a layer'],
  ['Delete', 'Delete the selected pixels'],
  ['Ctrl+V', 'Paste an image as a layer'],
  ['Enter', 'Apply crop or transform'],
  ['Esc', 'Cancel selection, crop or transform'],
  ['?', 'This list'],
];

export function ShortcutsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t('Keyboard shortcuts')} testId="shortcuts">
      <dl className="fs-ed__keys">
        {SHORTCUTS.map(([k, label]) => (
          <div key={k}>
            <dt>
              <kbd>{k}</kbd>
            </dt>
            <dd>{t(label)}</dd>
          </div>
        ))}
      </dl>
    </Dialog>
  );
}
