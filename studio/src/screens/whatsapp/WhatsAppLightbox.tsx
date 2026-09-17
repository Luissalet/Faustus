import * as RadixDialog from '@radix-ui/react-dialog';
import { Download, Minus, Plus, RotateCcw, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { IconButton } from '../../components';
import { t } from '../../i18n';

/**
 * A photo opened from a bubble: the same overlay WhatsApp Web uses instead
 * of a new tab. Radix owns focus, Escape and the inert background; the
 * zoom is ours — wheel or the +/− buttons around 1×…6×, drag to pan once
 * zoomed, double-click toggles 1× ↔ 2.5×. Nothing here fetches anything:
 * the `<img>` is the same media URL the bubble already showed.
 */

const MIN = 1;
const MAX = 6;
const STEP = 1.25;

export interface LightboxProps {
  src: string | null;
  caption?: string;
  onClose: () => void;
}

export function Lightbox({ src, caption, onClose }: LightboxProps) {
  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);

  // A fresh photo opens at 1×, centred.
  useEffect(() => {
    setScale(1);
    setOffset({ x: 0, y: 0 });
  }, [src]);

  const clamp = (v: number) => Math.min(MAX, Math.max(MIN, v));
  const zoomBy = useCallback((factor: number) => {
    setScale((s) => {
      const next = clamp(s * factor);
      if (next === 1) setOffset({ x: 0, y: 0 });
      return next;
    });
  }, []);

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    zoomBy(e.deltaY < 0 ? STEP : 1 / STEP);
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (scale === 1) return;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, ox: offset.x, oy: offset.y };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    setOffset({ x: drag.current.ox + (e.clientX - drag.current.x), y: drag.current.oy + (e.clientY - drag.current.y) });
  };
  const onPointerUp = () => {
    drag.current = null;
  };

  return (
    <RadixDialog.Root open={!!src} onOpenChange={(open) => { if (!open) onClose(); }}>
      <RadixDialog.Portal container={document.getElementById('fs-overlay-root') ?? undefined}>
        <RadixDialog.Overlay className="fs-overlay-backdrop fs-wa__lightbox-backdrop" />
        <RadixDialog.Content className="fs-wa__lightbox" data-testid="whatsapp-lightbox" aria-describedby={undefined}>
          <RadixDialog.Title className="fs-sr-only">{t('Photo')}</RadixDialog.Title>
          <div className="fs-wa__lightbox-bar">
            <span className="fs-wa__lightbox-zoom" aria-live="polite">{Math.round(scale * 100)}%</span>
            <IconButton icon={Minus} label={t('Zoom out')} onClick={() => zoomBy(1 / STEP)} disabled={scale <= MIN} testId="whatsapp-lightbox-out" />
            <IconButton icon={Plus} label={t('Zoom in')} onClick={() => zoomBy(STEP)} disabled={scale >= MAX} testId="whatsapp-lightbox-in" />
            <IconButton icon={RotateCcw} label={t('Reset zoom')} onClick={() => { setScale(1); setOffset({ x: 0, y: 0 }); }} disabled={scale === 1} />
            {src && (
              <a className="fs-wa__lightbox-download" href={src} download aria-label={t('Download')} title={t('Download')}>
                <Download size={16} aria-hidden="true" />
              </a>
            )}
            <RadixDialog.Close asChild>
              <IconButton icon={X} label={t('Close')} testId="whatsapp-lightbox-close" />
            </RadixDialog.Close>
          </div>
          <div
            className="fs-wa__lightbox-stage"
            data-zoomed={scale > 1 ? '' : undefined}
            onWheel={onWheel}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onDoubleClick={() => (scale === 1 ? setScale(2.5) : (setScale(1), setOffset({ x: 0, y: 0 })))}
          >
            {src && (
              <img
                src={src}
                alt={caption || ''}
                className="fs-wa__lightbox-img"
                draggable={false}
                style={{ transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})` }}
              />
            )}
          </div>
          {caption && <p className="fs-wa__lightbox-caption">{caption}</p>}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
