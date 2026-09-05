/**
 * Whole-document geometry: rotate, flip, resize, crop, upscale, and the
 * per-layer free transform. Ported from `canvas-transforms.js` and the
 * transform/crop tools of the legacy editor.
 */
import { ctx2d, makeCanvas } from './canvas';
import { invalidateAdjCache, type Doc } from './doc';

export function rotateDoc(doc: Doc, deg: 90 | 180 | 270): void {
  const oldW = doc.width, oldH = doc.height;
  const swap = deg === 90 || deg === 270;
  const newW = swap ? oldH : oldW, newH = swap ? oldW : oldH;
  const rad = (deg * Math.PI) / 180;
  const cos = Math.cos(rad), sin = Math.sin(rad);
  for (const layer of doc.layers) {
    const lw = layer.canvas.width, lh = layer.canvas.height;
    const cx = layer.offset.x + lw / 2, cy = layer.offset.y + lh / 2;
    const dx = cx - oldW / 2, dy = cy - oldH / 2;
    const nx = dx * cos - dy * sin + newW / 2;
    const ny = dx * sin + dy * cos + newH / 2;
    const newLw = swap ? lh : lw, newLh = swap ? lw : lh;
    const tmp = makeCanvas(newLw, newLh);
    const tctx = ctx2d(tmp);
    tctx.translate(newLw / 2, newLh / 2);
    tctx.rotate(rad);
    tctx.drawImage(layer.canvas, -lw / 2, -lh / 2);
    layer.canvas.width = newLw;
    layer.canvas.height = newLh;
    ctx2d(layer.canvas).drawImage(tmp, 0, 0);
    layer.offset = { x: Math.round(nx - newLw / 2), y: Math.round(ny - newLh / 2) };
    for (const m of layer.masks) {
      const mt = makeCanvas(newW, newH);
      const mctx = ctx2d(mt);
      mctx.translate(newW / 2, newH / 2);
      mctx.rotate(rad);
      mctx.drawImage(m.canvas, -oldW / 2, -oldH / 2);
      m.canvas = mt;
    }
    invalidateAdjCache(layer);
  }
  doc.width = newW;
  doc.height = newH;
}

export function flipDoc(doc: Doc, axis: 'h' | 'v'): void {
  for (const layer of doc.layers) {
    flipCanvas(layer.canvas, axis);
    for (const m of layer.masks) flipCanvas(m.canvas, axis);
    const lw = layer.canvas.width, lh = layer.canvas.height;
    layer.offset = axis === 'h' ? { x: doc.width - layer.offset.x - lw, y: layer.offset.y } : { x: layer.offset.x, y: doc.height - layer.offset.y - lh };
    invalidateAdjCache(layer);
  }
}

export function flipCanvas(canvas: HTMLCanvasElement, axis: 'h' | 'v'): void {
  const w = canvas.width, h = canvas.height;
  const tmp = makeCanvas(w, h);
  const tctx = ctx2d(tmp);
  if (axis === 'h') {
    tctx.translate(w, 0);
    tctx.scale(-1, 1);
  } else {
    tctx.translate(0, h);
    tctx.scale(1, -1);
  }
  tctx.drawImage(canvas, 0, 0);
  const ctx = ctx2d(canvas);
  ctx.clearRect(0, 0, w, h);
  ctx.drawImage(tmp, 0, 0);
}

/** Change the canvas bounds; layers keep their pixels, anchored per `anchor`. */
export function resizeDoc(doc: Doc, width: number, height: number, anchor: 'tl' | 'center' = 'center'): void {
  const dx = anchor === 'center' ? Math.round((width - doc.width) / 2) : 0;
  const dy = anchor === 'center' ? Math.round((height - doc.height) / 2) : 0;
  for (const layer of doc.layers) {
    layer.offset = { x: layer.offset.x + dx, y: layer.offset.y + dy };
    for (const m of layer.masks) {
      const mt = makeCanvas(width, height);
      ctx2d(mt).drawImage(m.canvas, dx, dy);
      m.canvas = mt;
    }
  }
  doc.width = width;
  doc.height = height;
}

/** Scale everything by `factor` (2 or 4) with high-quality resampling. */
export function scaleDoc(doc: Doc, factor: number): void {
  const newW = Math.round(doc.width * factor), newH = Math.round(doc.height * factor);
  for (const layer of doc.layers) {
    const lw = Math.round(layer.canvas.width * factor), lh = Math.round(layer.canvas.height * factor);
    const tmp = makeCanvas(lw, lh);
    const tctx = ctx2d(tmp);
    tctx.imageSmoothingEnabled = true;
    tctx.imageSmoothingQuality = 'high';
    tctx.drawImage(layer.canvas, 0, 0, lw, lh);
    layer.canvas = tmp;
    layer.offset = { x: Math.round(layer.offset.x * factor), y: Math.round(layer.offset.y * factor) };
    for (const m of layer.masks) {
      const mt = makeCanvas(newW, newH);
      ctx2d(mt).drawImage(m.canvas, 0, 0, newW, newH);
      m.canvas = mt;
    }
    invalidateAdjCache(layer);
  }
  doc.width = newW;
  doc.height = newH;
}

/** Crop the document to `rect` (document coordinates). */
export function cropDoc(doc: Doc, rect: { x: number; y: number; w: number; h: number }): void {
  const x = Math.round(rect.x), y = Math.round(rect.y);
  const w = Math.max(1, Math.round(rect.w)), h = Math.max(1, Math.round(rect.h));
  for (const layer of doc.layers) {
    layer.offset = { x: layer.offset.x - x, y: layer.offset.y - y };
    for (const m of layer.masks) {
      const mt = makeCanvas(w, h);
      ctx2d(mt).drawImage(m.canvas, -x, -y);
      m.canvas = mt;
    }
  }
  doc.width = w;
  doc.height = h;
}

/**
 * Bake a free transform into a layer: new size, rotation (deg) and flips
 * around the layer's centre. Returns the new offset so the centre stays put.
 */
export function transformLayerPixels(src: HTMLCanvasElement, opts: { width: number; height: number; rotation: number; flipH: boolean; flipV: boolean }): HTMLCanvasElement {
  const rad = (opts.rotation * Math.PI) / 180;
  const w = Math.max(1, Math.round(opts.width)), h = Math.max(1, Math.round(opts.height));
  const cos = Math.abs(Math.cos(rad)), sin = Math.abs(Math.sin(rad));
  const bw = Math.max(1, Math.ceil(w * cos + h * sin)), bh = Math.max(1, Math.ceil(w * sin + h * cos));
  const out = makeCanvas(bw, bh);
  const ctx = ctx2d(out);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.translate(bw / 2, bh / 2);
  ctx.rotate(rad);
  ctx.scale(opts.flipH ? -1 : 1, opts.flipV ? -1 : 1);
  ctx.drawImage(src, -w / 2, -h / 2, w, h);
  return out;
}
