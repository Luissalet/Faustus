/**
 * Whole-layer filters. Each takes a pristine snapshot and a destination
 * context so a dialog can preview live and commit once. Ported from the
 * legacy `filters/blur.js`.
 */
import { ctx2d, makeCanvas } from './canvas';

/** Edge-extended gaussian blur (no dark halo at the borders). */
export function gaussianBlur(snap: HTMLCanvasElement, radius: number, dst: CanvasRenderingContext2D): void {
  if (!radius || radius <= 0) {
    dst.drawImage(snap, 0, 0);
    return;
  }
  const w = snap.width, h = snap.height;
  const m = Math.ceil(radius * 2 + 4);
  const pad = makeCanvas(w + m * 2, h + m * 2);
  const pctx = ctx2d(pad);
  pctx.drawImage(snap, m, m);
  pctx.drawImage(snap, 0, 0, w, 1, m, 0, w, m);
  pctx.drawImage(snap, 0, h - 1, w, 1, m, m + h, w, m);
  pctx.drawImage(snap, 0, 0, 1, h, 0, m, m, h);
  pctx.drawImage(snap, w - 1, 0, 1, h, m + w, m, m, h);
  pctx.drawImage(snap, 0, 0, 1, 1, 0, 0, m, m);
  pctx.drawImage(snap, w - 1, 0, 1, 1, m + w, 0, m, m);
  pctx.drawImage(snap, 0, h - 1, 1, 1, 0, m + h, m, m);
  pctx.drawImage(snap, w - 1, h - 1, 1, 1, m + w, m + h, m, m);
  const out = makeCanvas(pad.width, pad.height);
  const octx = ctx2d(out);
  octx.filter = `blur(${radius}px)`;
  octx.drawImage(pad, 0, 0);
  octx.filter = 'none';
  dst.drawImage(out, m, m, w, h, 0, 0, w, h);
}

/** Radial zoom blur from the centre; `strength` 0..100. */
export function zoomBlur(snap: HTMLCanvasElement, strength: number, dst: CanvasRenderingContext2D): void {
  const w = snap.width, h = snap.height;
  const steps = 16;
  dst.drawImage(snap, 0, 0);
  dst.globalAlpha = 0.18;
  for (let s = 1; s <= steps; s++) {
    const t = s / steps;
    const scale = 1 + (strength / 200) * t;
    const sw = w * scale, sh = h * scale;
    dst.drawImage(snap, (w - sw) / 2, (h - sh) / 2, sw, sh);
  }
  dst.globalAlpha = 1;
}

/** Directional blur: `angle` degrees, `length` px. */
export function motionBlur(snap: HTMLCanvasElement, angle: number, length: number, dst: CanvasRenderingContext2D): void {
  const w = snap.width, h = snap.height;
  const rad = (angle * Math.PI) / 180;
  const dx = Math.cos(rad), dy = Math.sin(rad);
  const steps = Math.max(4, Math.min(80, Math.round(length)));
  const acc = makeCanvas(w, h);
  const actx = ctx2d(acc);
  actx.globalCompositeOperation = 'lighter';
  actx.globalAlpha = 1 / steps;
  for (let i = 0; i < steps; i++) {
    const t = i / Math.max(1, steps - 1) - 0.5;
    actx.drawImage(snap, dx * length * t, dy * length * t);
  }
  actx.globalCompositeOperation = 'source-over';
  actx.globalAlpha = 1;
  dst.drawImage(acc, 0, 0);
}
