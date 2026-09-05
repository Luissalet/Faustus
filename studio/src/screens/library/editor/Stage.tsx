import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { cursorForHandle } from '../../../lib/pixel';
import { t } from '../../../i18n';
import type { PixelEditor } from './engine';

interface Props {
  ed: PixelEditor;
  version: number;
  onDropFiles: (files: File[]) => void;
}

const MIN_ZOOM = 0.05;
const MAX_ZOOM = 32;

/**
 * The picture: two canvases (pixels + overlay) inside a pannable, zoomable
 * viewport. Pointer events reach the engine in document coordinates; the
 * stage owns only zoom, pan and the cursor.
 */
export function Stage({ ed, version, onDropFiles }: Props) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLCanvasElement>(null);
  const overlayRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [space, setSpace] = useState(false);
  const [panning, setPanning] = useState(false);
  const [dropping, setDropping] = useState(false);
  const panStart = useRef<{ x: number; y: number; px: number; py: number } | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinch = useRef<{ dist: number; zoom: number; mid: { x: number; y: number }; px: number; py: number } | null>(null);

  useLayoutEffect(() => {
    const main = mainRef.current, overlay = overlayRef.current, vp = viewportRef.current;
    if (!main || !overlay || !vp) return;
    const css = getComputedStyle(vp);
    ed.attach(main, overlay, {
      selection: css.getPropertyValue('--fs-ed-selection').trim() || css.color,
      mask: css.getPropertyValue('--fs-ed-mask').trim() || css.color,
      guide: css.getPropertyValue('--fs-ed-guide').trim() || css.color,
      handle: css.getPropertyValue('--fs-ed-handle').trim() || css.color,
      crop: css.getPropertyValue('--fs-ed-handle').trim() || css.color,
      clone: css.getPropertyValue('--fs-ed-selection').trim() || css.color,
    });
  }, [ed]);

  useEffect(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    const ro = new ResizeObserver(() => setSize({ w: vp.clientWidth, h: vp.clientHeight }));
    ro.observe(vp);
    setSize({ w: vp.clientWidth, h: vp.clientHeight });
    return () => ro.disconnect();
  }, []);

  const fit = useCallback(() => {
    if (!size.w || !size.h) return;
    const pad = 32;
    const z = Math.min((size.w - pad) / ed.doc.width, (size.h - pad) / ed.doc.height, 2);
    ed.zoom = Math.max(MIN_ZOOM, z);
    ed.panX = Math.round((size.w - ed.doc.width * ed.zoom) / 2);
    ed.panY = Math.round((size.h - ed.doc.height * ed.zoom) / 2);
    ed.fitMode = 'fit';
    ed.drawOverlay();
    ed.notify();
  }, [ed, size.w, size.h]);

  // Fit whenever the document or the viewport changes size while in fit mode.
  const docW = ed.doc.width, docH = ed.doc.height;
  useEffect(() => {
    if (ed.fitMode === 'fit') fit();
  }, [fit, docW, docH, ed.fitMode]);

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.code === 'Space' && !isTyping(e)) {
        setSpace(true);
        e.preventDefault();
      }
    };
    const up = (e: KeyboardEvent) => {
      if (e.code === 'Space') setSpace(false);
    };
    window.addEventListener('keydown', down);
    window.addEventListener('keyup', up);
    return () => {
      window.removeEventListener('keydown', down);
      window.removeEventListener('keyup', up);
    };
  }, []);

  const toDoc = useCallback(
    (clientX: number, clientY: number) => {
      const vp = viewportRef.current;
      if (!vp) return { x: 0, y: 0 };
      const rect = vp.getBoundingClientRect();
      return { x: (clientX - rect.left - ed.panX) / ed.zoom, y: (clientY - rect.top - ed.panY) / ed.zoom };
    },
    [ed],
  );

  const zoomAt = useCallback(
    (factor: number, clientX?: number, clientY?: number) => {
      const vp = viewportRef.current;
      if (!vp) return;
      const rect = vp.getBoundingClientRect();
      const cx = clientX === undefined ? rect.width / 2 : clientX - rect.left;
      const cy = clientY === undefined ? rect.height / 2 : clientY - rect.top;
      const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, ed.zoom * factor));
      const k = next / ed.zoom;
      ed.panX = Math.round(cx - (cx - ed.panX) * k);
      ed.panY = Math.round(cy - (cy - ed.panY) * k);
      ed.zoom = next;
      ed.fitMode = 'custom';
      ed.drawOverlay();
      ed.notify();
    },
    [ed],
  );

  useEffect(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      if (e.ctrlKey || e.metaKey || !e.shiftKey) {
        zoomAt(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX, e.clientY);
      } else {
        ed.panX -= e.deltaY;
        ed.fitMode = 'custom';
        ed.notify();
      }
    };
    vp.addEventListener('wheel', onWheel, { passive: false });
    return () => vp.removeEventListener('wheel', onWheel);
  }, [ed, zoomAt]);

  const mods = (e: React.PointerEvent) => ({ shift: e.shiftKey, alt: e.altKey, ctrl: e.ctrlKey || e.metaKey });

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const vp = viewportRef.current;
    if (!vp) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      pinch.current = { dist: Math.hypot(a.x - b.x, a.y - b.y), zoom: ed.zoom, mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, px: ed.panX, py: ed.panY };
      ed.pointerUp(toDoc(e.clientX, e.clientY));
      return;
    }
    vp.setPointerCapture(e.pointerId);
    if (space || e.button === 1) {
      panStart.current = { x: e.clientX, y: e.clientY, px: ed.panX, py: ed.panY };
      setPanning(true);
      e.preventDefault();
      return;
    }
    if (e.button === 2 && ed.tool !== 'clone') return;
    e.preventDefault();
    ed.pointerDown(toDoc(e.clientX, e.clientY), mods(e), e.button);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (pointers.current.has(e.pointerId)) pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch.current && pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, (pinch.current.zoom * dist) / Math.max(1, pinch.current.dist)));
      const vp = viewportRef.current;
      if (!vp) return;
      const rect = vp.getBoundingClientRect();
      const mid = { x: (a.x + b.x) / 2 - rect.left, y: (a.y + b.y) / 2 - rect.top };
      const m0 = { x: pinch.current.mid.x - rect.left, y: pinch.current.mid.y - rect.top };
      const k = next / pinch.current.zoom;
      ed.panX = Math.round(mid.x - (m0.x - pinch.current.px) * k);
      ed.panY = Math.round(mid.y - (m0.y - pinch.current.py) * k);
      ed.zoom = next;
      ed.fitMode = 'custom';
      ed.drawOverlay();
      ed.notify();
      return;
    }
    if (panStart.current) {
      ed.panX = panStart.current.px + (e.clientX - panStart.current.x);
      ed.panY = panStart.current.py + (e.clientY - panStart.current.y);
      ed.fitMode = 'custom';
      ed.notify();
      return;
    }
    ed.pointerMove(toDoc(e.clientX, e.clientY), mods(e));
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
    if (panStart.current) {
      panStart.current = null;
      setPanning(false);
      return;
    }
    ed.pointerUp(toDoc(e.clientX, e.clientY));
  };

  const cursor = panning ? 'grabbing' : space ? 'grab' : ed.tool === 'transform' ? cursorForHandle(ed.hoveredHandle) : ed.tool === 'move' ? 'move' : ed.tool === 'crop' ? 'crosshair' : ed.tool === 'brush' || ed.tool === 'eraser' || ed.tool === 'clone' || ed.tool === 'inpaint' ? 'none' : 'crosshair';

  return (
    <div
      ref={viewportRef}
      className="fs-ed__viewport"
      data-testid="editor-stage"
      data-dropping={dropping || undefined}
      style={{ cursor }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onPointerLeave={() => ed.pointerLeave()}
      onContextMenu={(e) => e.preventDefault()}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes('Files')) {
          e.preventDefault();
          setDropping(true);
        }
      }}
      onDragLeave={() => setDropping(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDropping(false);
        const files = [...e.dataTransfer.files].filter((f) => f.type.startsWith('image/'));
        if (files.length) onDropFiles(files);
      }}
      role="img"
      aria-label={t('Canvas, {w} by {h} pixels', { w: ed.doc.width, h: ed.doc.height })}
    >
      <div className="fs-ed__paper" style={{ transform: `translate(${ed.panX}px, ${ed.panY}px) scale(${ed.zoom})`, width: ed.doc.width, height: ed.doc.height }} data-pixelated={ed.zoom >= 3 || undefined}>
        <canvas ref={mainRef} className="fs-ed__canvas" />
        <canvas ref={overlayRef} className="fs-ed__overlay" />
      </div>
      {dropping && <p className="fs-ed__drop">{t('Drop to add as a layer')}</p>}
    </div>
  );
}

export function stageZoom(ed: PixelEditor) {
  return {
    zoomIn: () => zoomBy(ed, 1.25),
    zoomOut: () => zoomBy(ed, 1 / 1.25),
    actual: () => {
      const k = 1 / ed.zoom;
      const cx = ed.panX + (ed.doc.width * ed.zoom) / 2, cy = ed.panY + (ed.doc.height * ed.zoom) / 2;
      ed.zoom = 1;
      ed.panX = Math.round(cx - (cx - ed.panX) * k);
      ed.panY = Math.round(cy - (cy - ed.panY) * k);
      ed.fitMode = 'actual';
      ed.drawOverlay();
      ed.notify();
    },
    fit: () => {
      ed.fitMode = 'fit';
      ed.notify();
    },
  };
}

function zoomBy(ed: PixelEditor, factor: number): void {
  const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, ed.zoom * factor));
  const k = next / ed.zoom;
  const cx = ed.panX + (ed.doc.width * ed.zoom) / 2, cy = ed.panY + (ed.doc.height * ed.zoom) / 2;
  ed.panX = Math.round(cx - (cx - ed.panX) * k);
  ed.panY = Math.round(cy - (cy - ed.panY) * k);
  ed.zoom = next;
  ed.fitMode = 'custom';
  ed.drawOverlay();
  ed.notify();
}

export function isTyping(e: KeyboardEvent | React.KeyboardEvent): boolean {
  const el = e.target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}
