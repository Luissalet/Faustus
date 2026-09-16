import { useCallback, useEffect, useRef, useState } from 'react';
import type { CanvasDocContent, CanvasLayer } from '../../adapters/creator_canvas';

/**
 * WP20 — The actual `<canvas>` (HTML canvas, per the ficha: "lienzo HTML
 * canvas") a person paints/selects/drags on. It draws an APPROXIMATE local
 * preview (loading each raster layer's own asset via
 * `/api/artifacts/{id}/download`, applying offset/scale/rotation/opacity
 * client-side) — the pixel-exact composition is always
 * `routes/creator_canvas_routes.py`'s server-side `render()`
 * (`adapters/creator_canvas.ts::renderUrl`), which `Canvas.tsx` shows as
 * the base picture underneath. Every gesture here (move, scale/rotate via
 * handles, close a polygon mask) ends as ONE typed `canvas.*` command
 * carrying the document's OWN `expected_revision` — nothing is applied to
 * server state optimistically, matching `TimelineTrack.tsx`'s "commit
 * server-validated, never assume" pattern.
 */

export interface LoadedImage {
  img: HTMLImageElement;
  width: number;
  height: number;
}

const HANDLE_SIZE = 10;

export interface CanvasLayersProps {
  content: CanvasDocContent;
  baseImageUrl: string;
  selectedLayerId: string | null;
  onSelectLayer: (id: string | null) => void;
  /** Absolute offset, committed on pointer up. */
  onCommitMove: (layerId: string, offset: { x: number; y: number }) => void;
  /** Absolute scale (+ optional absolute rotation), committed on pointer up. */
  onCommitTransform: (layerId: string, scale: number, degrees?: number) => void;
  maskMode: boolean;
  onCommitPolygonMask: (layerId: string, points: { x: number; y: number }[]) => void;
  zoom: number;
  disabled?: boolean;
}

function layerBounds(layer: CanvasLayer, natural: { w: number; h: number }) {
  const scale = layer.scale ?? 1;
  const w = natural.w * scale;
  const h = natural.h * scale;
  const cx = layer.offset.x + natural.w / 2;
  const cy = layer.offset.y + natural.h / 2;
  return { x: cx - w / 2, y: cy - h / 2, w, h, cx, cy };
}

export function CanvasLayers({
  content, baseImageUrl, selectedLayerId, onSelectLayer, onCommitMove, onCommitTransform,
  maskMode, onCommitPolygonMask, zoom, disabled = false,
}: CanvasLayersProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [images, setImages] = useState<Record<string, LoadedImage>>({});
  const [baseImg, setBaseImg] = useState<HTMLImageElement | null>(null);
  const [drag, setDrag] = useState<
    | { kind: 'move'; layerId: string; startX: number; startY: number; origin: { x: number; y: number }; preview: { x: number; y: number } }
    | { kind: 'scale'; layerId: string; startX: number; startY: number; originScale: number; preview: number }
    | null
  >(null);
  const [polygonPoints, setPolygonPoints] = useState<{ x: number; y: number }[]>([]);

  // Load the base render.
  useEffect(() => {
    let cancelled = false;
    const img = new Image();
    img.onload = () => { if (!cancelled) setBaseImg(img); };
    img.src = baseImageUrl;
    return () => { cancelled = true; };
  }, [baseImageUrl]);

  // Load every raster layer's own asset for the interactive preview.
  useEffect(() => {
    let cancelled = false;
    const next: Record<string, LoadedImage> = {};
    let pending = 0;
    for (const layer of content.layers) {
      if (!layer.asset_ref || layer.kind === 'mask') continue;
      pending += 1;
      const img = new Image();
      img.onload = () => {
        next[layer.id] = { img, width: img.naturalWidth, height: img.naturalHeight };
        pending -= 1;
        if (pending === 0 && !cancelled) setImages({ ...next });
      };
      img.onerror = () => { pending -= 1; if (pending === 0 && !cancelled) setImages({ ...next }); };
      img.src = `/api/artifacts/${encodeURIComponent(layer.asset_ref)}/download`;
    }
    if (pending === 0) setImages({});
    return () => { cancelled = true; };
  }, [content.layers]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    canvas.width = content.width * zoom;
    canvas.height = content.height * zoom;
    ctx.save();
    ctx.scale(zoom, zoom);
    ctx.clearRect(0, 0, content.width, content.height);

    if (baseImg) ctx.drawImage(baseImg, 0, 0, content.width, content.height);

    for (const layer of content.layers) {
      if (!layer.visible || layer.kind === 'mask') continue;
      const loaded = images[layer.id];
      if (!loaded) continue;
      const isDraggingThis = drag && drag.layerId === layer.id;
      const offset = isDraggingThis && drag.kind === 'move' ? drag.preview : layer.offset;
      const scale = isDraggingThis && drag.kind === 'scale' ? drag.preview : (layer.scale ?? 1);
      const w = loaded.width * scale;
      const h = loaded.height * scale;
      const cx = offset.x + loaded.width / 2;
      const cy = offset.y + loaded.height / 2;
      ctx.save();
      ctx.globalAlpha = layer.opacity;
      ctx.translate(cx, cy);
      if (layer.rotation_degrees) ctx.rotate((layer.rotation_degrees * Math.PI) / 180);
      ctx.drawImage(loaded.img, -w / 2, -h / 2, w, h);
      ctx.restore();

      if (layer.id === selectedLayerId) {
        const b = layerBounds({ ...layer, offset, scale }, { w: loaded.width, h: loaded.height });
        ctx.save();
        ctx.strokeStyle = '#6ea8fe';
        ctx.lineWidth = 1 / zoom;
        ctx.strokeRect(b.x, b.y, b.w, b.h);
        ctx.fillStyle = '#6ea8fe';
        const handleSize = HANDLE_SIZE / zoom;
        ctx.fillRect(b.x + b.w - handleSize / 2, b.y + b.h - handleSize / 2, handleSize, handleSize);
        ctx.restore();
      }
    }

    if (maskMode && polygonPoints.length > 0) {
      ctx.save();
      ctx.strokeStyle = '#ffb020';
      ctx.fillStyle = 'rgba(255, 176, 32, 0.25)';
      ctx.lineWidth = 2 / zoom;
      ctx.beginPath();
      ctx.moveTo(polygonPoints[0].x, polygonPoints[0].y);
      for (const p of polygonPoints.slice(1)) ctx.lineTo(p.x, p.y);
      if (polygonPoints.length > 2) ctx.closePath();
      ctx.fill();
      ctx.stroke();
      for (const p of polygonPoints) {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3 / zoom, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }

    ctx.restore();
  }, [content, images, baseImg, selectedLayerId, drag, zoom, maskMode, polygonPoints]);

  useEffect(() => { draw(); }, [draw]);

  const toDocCoords = useCallback((e: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    return { x: (e.clientX - rect.left) / zoom, y: (e.clientY - rect.top) / zoom };
  }, [zoom]);

  const hitTestLayer = useCallback((x: number, y: number): { layerId: string; onHandle: boolean } | null => {
    for (let i = content.layers.length - 1; i >= 0; i -= 1) {
      const layer = content.layers[i];
      if (!layer.visible || layer.kind === 'mask') continue;
      const loaded = images[layer.id];
      if (!loaded) continue;
      const b = layerBounds(layer, { w: loaded.width, h: loaded.height });
      const handleSize = HANDLE_SIZE / zoom;
      const onHandle = x >= b.x + b.w - handleSize && x <= b.x + b.w + handleSize
        && y >= b.y + b.h - handleSize && y <= b.y + b.h + handleSize;
      if (onHandle) return { layerId: layer.id, onHandle: true };
      if (x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h) return { layerId: layer.id, onHandle: false };
    }
    return null;
  }, [content.layers, images, zoom]);

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLCanvasElement>) => {
    if (disabled) return;
    const { x, y } = toDocCoords(e);
    if (maskMode) {
      if (!selectedLayerId) return;
      setPolygonPoints((prev) => [...prev, { x, y }]);
      return;
    }
    const hit = hitTestLayer(x, y);
    if (!hit) { onSelectLayer(null); return; }
    onSelectLayer(hit.layerId);
    const layer = content.layers.find((l) => l.id === hit.layerId);
    if (!layer) return;
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    if (hit.onHandle) {
      setDrag({ kind: 'scale', layerId: hit.layerId, startX: x, startY: y, originScale: layer.scale ?? 1, preview: layer.scale ?? 1 });
    } else {
      setDrag({ kind: 'move', layerId: hit.layerId, startX: x, startY: y, origin: layer.offset, preview: layer.offset });
    }
  }, [disabled, maskMode, selectedLayerId, toDocCoords, hitTestLayer, onSelectLayer, content.layers]);

  const onPointerMove = useCallback((e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!drag) return;
    const { x, y } = toDocCoords(e);
    if (drag.kind === 'move') {
      const dx = x - drag.startX;
      const dy = y - drag.startY;
      setDrag({ ...drag, preview: { x: Math.round(drag.origin.x + dx), y: Math.round(drag.origin.y + dy) } });
    } else {
      const loaded = images[drag.layerId];
      const layer = content.layers.find((l) => l.id === drag.layerId);
      if (!loaded || !layer) return;
      const b = layerBounds(layer, { w: loaded.width, h: loaded.height });
      const dist = Math.max(4, Math.hypot(x - b.cx, y - b.cy));
      const originDist = Math.max(4, Math.hypot(loaded.width, loaded.height) * (drag.originScale) / 2);
      const nextScale = Math.max(0.05, dist / (originDist / drag.originScale));
      setDrag({ ...drag, preview: Math.round(nextScale * 100) / 100 });
    }
  }, [drag, toDocCoords, images, content.layers]);

  const onPointerUp = useCallback(() => {
    if (!drag) return;
    if (drag.kind === 'move') onCommitMove(drag.layerId, drag.preview);
    else onCommitTransform(drag.layerId, drag.preview);
    setDrag(null);
  }, [drag, onCommitMove, onCommitTransform]);

  const onDoubleClick = useCallback(() => {
    if (!maskMode || !selectedLayerId || polygonPoints.length < 3) return;
    onCommitPolygonMask(selectedLayerId, polygonPoints);
    setPolygonPoints([]);
  }, [maskMode, selectedLayerId, polygonPoints, onCommitPolygonMask]);

  return (
    <canvas
      ref={canvasRef}
      data-testid="canvas-surface"
      role="img"
      aria-label="Canvas"
      style={{ touchAction: 'none', cursor: maskMode ? 'crosshair' : 'default' }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onDoubleClick={onDoubleClick}
    />
  );
}
