/**
 * Mask maths: flood fill, dilate/erode, feathering and lasso rasterising.
 * Ported from the legacy editor (`mask-utils.js`, `tools/flood-fill.js`,
 * `tools/lasso-mask.js`, `ai-rembg.js` edge cleanup). Masks are canvases
 * where alpha = selection strength; helpers always return a fresh canvas.
 */
import { ctx2d, makeCanvas } from './canvas';

export interface Point {
  x: number;
  y: number;
}

/** Dilate (px > 0) or erode (px < 0) a binary alpha mask by blur + threshold. */
export function dilateMask(src: HTMLCanvasElement, px: number): HTMLCanvasElement {
  const w = src.width, h = src.height;
  const out = makeCanvas(w, h);
  const ctx = ctx2d(out);
  if (px === 0) {
    ctx.drawImage(src, 0, 0);
    return out;
  }
  const dilate = px > 0;
  ctx.filter = `blur(${Math.abs(px)}px)`;
  ctx.drawImage(src, 0, 0);
  ctx.filter = 'none';
  const img = ctx.getImageData(0, 0, w, h);
  const threshold = dilate ? 8 : 247;
  for (let i = 0; i < img.data.length; i += 4) {
    const a = img.data[i + 3];
    const keep = dilate ? a > threshold : a >= threshold;
    if (keep) {
      img.data[i] = img.data[i + 1] = img.data[i + 2] = 255;
      img.data[i + 3] = 255;
    } else {
      img.data[i + 3] = 0;
    }
  }
  ctx.putImageData(img, 0, 0);
  return out;
}

/** Gaussian-soften a mask's alpha. 0 returns a copy. */
export function featherMask(src: HTMLCanvasElement, px: number): HTMLCanvasElement {
  const out = makeCanvas(src.width, src.height);
  const ctx = ctx2d(out);
  if (px > 0) ctx.filter = `blur(${px}px)`;
  ctx.drawImage(src, 0, 0);
  ctx.filter = 'none';
  return out;
}

/** White where `src` is transparent and vice versa. */
export function invertMask(src: HTMLCanvasElement): HTMLCanvasElement {
  const w = src.width, h = src.height;
  const out = makeCanvas(w, h);
  const ctx = ctx2d(out);
  const img = ctx2d(src).getImageData(0, 0, w, h);
  const d = img.data;
  for (let i = 0; i < d.length; i += 4) {
    const a = 255 - d[i + 3];
    d[i] = d[i + 1] = d[i + 2] = 255;
    d[i + 3] = a;
  }
  ctx.putImageData(img, 0, 0);
  return out;
}

export function combineMasks(base: HTMLCanvasElement | null, next: HTMLCanvasElement, mode: 'replace' | 'add' | 'subtract'): HTMLCanvasElement {
  if (!base || mode === 'replace') {
    const out = makeCanvas(next.width, next.height);
    ctx2d(out).drawImage(next, 0, 0);
    return out;
  }
  const out = makeCanvas(base.width, base.height);
  const ctx = ctx2d(out);
  ctx.drawImage(base, 0, 0);
  ctx.globalCompositeOperation = mode === 'add' ? 'lighter' : 'destination-out';
  ctx.drawImage(next, 0, 0);
  ctx.globalCompositeOperation = 'source-over';
  return out;
}

/** Solid white mask the size of the document. */
export function fullMask(w: number, h: number): HTMLCanvasElement {
  const out = makeCanvas(w, h);
  const ctx = ctx2d(out);
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, w, h);
  return out;
}

/**
 * Iterative 4-connected flood fill on RGBA bytes. Returns a `w × h` mask
 * with white where the fill landed, or null if the seed is out of bounds.
 * Tolerance 0..100 maps to a squared RGB+A distance (≈195k at 100).
 */
export function floodFillMask(src: Uint8ClampedArray, w: number, h: number, seedX: number, seedY: number, tolerance: number): HTMLCanvasElement | null {
  if (seedX < 0 || seedY < 0 || seedX >= w || seedY >= h) return null;
  const seedIdx = (seedY * w + seedX) * 4;
  const sr = src[seedIdx], sg = src[seedIdx + 1], sb = src[seedIdx + 2], sa = src[seedIdx + 3];
  const tol = Math.pow(tolerance * 4.42, 2);
  const visited = new Uint8Array(w * h);
  const stack: number[] = [seedX, seedY];
  visited[seedY * w + seedX] = 1;
  while (stack.length) {
    const y = stack.pop() as number;
    const x = stack.pop() as number;
    const nbrs = [x + 1, y, x - 1, y, x, y + 1, x, y - 1];
    for (let k = 0; k < 8; k += 2) {
      const nx = nbrs[k], ny = nbrs[k + 1];
      if (nx < 0 || ny < 0 || nx >= w || ny >= h) continue;
      const idx = ny * w + nx;
      if (visited[idx]) continue;
      const o = idx * 4;
      const dr = src[o] - sr, dg = src[o + 1] - sg, db = src[o + 2] - sb, da = src[o + 3] - sa;
      if (dr * dr + dg * dg + db * db + da * da <= tol) {
        visited[idx] = 1;
        stack.push(nx, ny);
      }
    }
  }
  const mask = makeCanvas(w, h);
  const mCtx = ctx2d(mask);
  const mData = mCtx.createImageData(w, h);
  for (let i = 0; i < w * h; i++) {
    if (visited[i]) {
      const o = i * 4;
      mData.data[o] = mData.data[o + 1] = mData.data[o + 2] = 255;
      mData.data[o + 3] = 255;
    }
  }
  mCtx.putImageData(mData, 0, 0);
  return mask;
}

/** Shift each polygon vertex along its outward normal by `grow` px. */
export function lassoOffsetPoints(points: Point[], grow: number): Point[] {
  const n = points.length;
  if (n < 3 || !grow) return points;
  let area = 0;
  for (let i = 0; i < n; i++) {
    const p = points[i], q = points[(i + 1) % n];
    area += (q.x - p.x) * (q.y + p.y);
  }
  const sign = area > 0 ? 1 : -1;
  const out: Point[] = new Array(n);
  for (let i = 0; i < n; i++) {
    const a = points[(i - 1 + n) % n], b = points[i], c = points[(i + 1) % n];
    const e1x = b.x - a.x, e1y = b.y - a.y, e2x = c.x - b.x, e2y = c.y - b.y;
    const l1 = Math.hypot(e1x, e1y) || 1, l2 = Math.hypot(e2x, e2y) || 1;
    const n1x = (e1y / l1) * sign, n1y = (-e1x / l1) * sign;
    const n2x = (e2y / l2) * sign, n2y = (-e2x / l2) * sign;
    const nx = (n1x + n2x) / 2, ny = (n1y + n2y) / 2;
    const nl = Math.hypot(nx, ny) || 1;
    out[i] = { x: b.x + (nx / nl) * grow, y: b.y + (ny / nl) * grow };
  }
  return out;
}

export function tracePolygon(ctx: CanvasRenderingContext2D, points: Point[], offX = 0, offY = 0): void {
  if (!points.length) return;
  ctx.beginPath();
  ctx.moveTo(points[0].x - offX, points[0].y - offY);
  for (let i = 1; i < points.length; i++) ctx.lineTo(points[i].x - offX, points[i].y - offY);
  ctx.closePath();
}

/**
 * Rasterise a lasso polygon into a `w × h` mask, optionally grown/shrunk
 * (blur + threshold) and feathered with a chamfer distance transform.
 */
export function buildLassoMask(points: Point[], w: number, h: number, offX: number, offY: number, feather: number, grow: number): HTMLCanvasElement {
  const hard = makeCanvas(w, h);
  const hCtx = ctx2d(hard);
  tracePolygon(hCtx, points, offX, offY);
  hCtx.fillStyle = '#fff';
  hCtx.fill();
  if (grow) {
    const blurC = makeCanvas(w, h);
    const bctx = ctx2d(blurC);
    bctx.filter = `blur(${Math.abs(grow)}px)`;
    bctx.drawImage(hard, 0, 0);
    bctx.filter = 'none';
    const blurred = bctx.getImageData(0, 0, w, h).data;
    const hd = hCtx.getImageData(0, 0, w, h);
    const out = hd.data;
    const thr = grow > 0 ? 32 : 200;
    for (let i = 0; i < out.length; i += 4) {
      const a = blurred[i + 3] >= thr ? 255 : 0;
      out[i] = out[i + 1] = out[i + 2] = a;
      out[i + 3] = a;
    }
    hCtx.putImageData(hd, 0, 0);
  }
  if (feather <= 0) return hard;
  return chamferFeather(hard, feather);
}

/** Distance-based feather of a binary mask (alpha ramps over `feather` px). */
export function chamferFeather(hard: HTMLCanvasElement, feather: number): HTMLCanvasElement {
  const w = hard.width, h = hard.height;
  const d = ctx2d(hard).getImageData(0, 0, w, h).data;
  const inside = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) inside[i] = d[i * 4 + 3] > 128 ? 1 : 0;
  const dist = new Float32Array(w * h);
  dist.fill(feather + 1);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x;
      if (!inside[i]) {
        dist[i] = 0;
        continue;
      }
      const edge = (x > 0 && !inside[i - 1]) || (x < w - 1 && !inside[i + 1]) || (y > 0 && !inside[(y - 1) * w + x]) || (y < h - 1 && !inside[(y + 1) * w + x]);
      if (edge) dist[i] = 1;
    }
  }
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x;
      if (dist[i] === 0) continue;
      if (x > 0) dist[i] = Math.min(dist[i], dist[i - 1] + 1);
      if (y > 0) dist[i] = Math.min(dist[i], dist[(y - 1) * w + x] + 1);
    }
  }
  for (let y = h - 1; y >= 0; y--) {
    for (let x = w - 1; x >= 0; x--) {
      const i = y * w + x;
      if (dist[i] === 0) continue;
      if (x < w - 1) dist[i] = Math.min(dist[i], dist[i + 1] + 1);
      if (y < h - 1) dist[i] = Math.min(dist[i], dist[(y + 1) * w + x] + 1);
    }
  }
  const result = makeCanvas(w, h);
  const rCtx = ctx2d(result);
  const rData = rCtx.createImageData(w, h);
  for (let i = 0; i < w * h; i++) {
    if (!inside[i]) continue;
    const alpha = dist[i] >= feather ? 255 : Math.round((dist[i] / feather) * 255);
    const o = i * 4;
    rData.data[o] = rData.data[o + 1] = rData.data[o + 2] = 255;
    rData.data[o + 3] = alpha;
  }
  rCtx.putImageData(rData, 0, 0);
  return result;
}

/**
 * Edge cleanup for a cut-out (bg removed) layer: nudge the alpha edge in
 * (grow < 0) or out (grow > 0), then feather it. Returns a fresh canvas
 * built from the pristine `snap`, so sliders can be dragged live.
 */
export function shapeEdge(snap: HTMLCanvasElement, feather: number, grow: number): HTMLCanvasElement {
  const w = snap.width, h = snap.height;
  let cur = makeCanvas(w, h);
  ctx2d(cur).drawImage(snap, 0, 0);
  if (grow !== 0) {
    const blurC = makeCanvas(w, h);
    const bctx = ctx2d(blurC);
    bctx.filter = `blur(${Math.abs(grow)}px)`;
    bctx.drawImage(snap, 0, 0);
    bctx.filter = 'none';
    const blurred = bctx.getImageData(0, 0, w, h).data;
    const lctx = ctx2d(cur);
    const layerData = lctx.getImageData(0, 0, w, h);
    const out = layerData.data;
    const thr = grow > 0 ? 32 : 200;
    for (let i = 0; i < out.length; i += 4) out[i + 3] = blurred[i + 3] >= thr ? 255 : 0;
    lctx.putImageData(layerData, 0, 0);
  }
  if (feather > 0) cur = featherMask(cur, feather);
  return cur;
}

/** Keep only the pixels of `layer` under `mask` (alpha multiply). */
export function applyMaskAlpha(target: HTMLCanvasElement, mask: HTMLCanvasElement, dx = 0, dy = 0): void {
  const ctx = ctx2d(target);
  ctx.save();
  ctx.globalCompositeOperation = 'destination-in';
  ctx.drawImage(mask, dx, dy);
  ctx.restore();
}

/** Remove the pixels of `target` under `mask`. */
export function cutMaskAlpha(target: HTMLCanvasElement, mask: HTMLCanvasElement, dx = 0, dy = 0): void {
  const ctx = ctx2d(target);
  ctx.save();
  ctx.globalCompositeOperation = 'destination-out';
  ctx.drawImage(mask, dx, dy);
  ctx.restore();
}
