/**
 * Brush, eraser, mask-paint and clone-stamp segments. Ported from the
 * legacy `stroke-pipeline.js`; each call paints last → current onto a
 * context and nothing else.
 */
import { ctx2d, makeCanvas } from './canvas';

export interface StrokeStyle {
  size: number;
  color: string;
  opacity: number; // 0..1
  flow: number; // 0..1
  softness: number; // 0..1
  mode: 'paint' | 'erase';
}

export function strokeSegment(ctx: CanvasRenderingContext2D, from: { x: number; y: number }, to: { x: number; y: number }, style: StrokeStyle): void {
  ctx.save();
  ctx.lineWidth = style.size;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.globalCompositeOperation = style.mode === 'erase' ? 'destination-out' : 'source-over';
  ctx.strokeStyle = style.mode === 'erase' ? 'rgba(0,0,0,1)' : style.color;
  ctx.globalAlpha = style.opacity * style.flow;
  if (style.softness > 0) {
    const blurPx = style.softness * (style.size / 2);
    ctx.filter = `blur(${blurPx.toFixed(2)}px)`;
  }
  ctx.beginPath();
  ctx.moveTo(from.x, from.y);
  ctx.lineTo(to.x, to.y);
  ctx.stroke();
  ctx.restore();
}

export interface CloneStyle {
  size: number;
  opacity: number;
  flow: number;
  softness: number; // 0..1
}

/**
 * Stamp `source` pixels along last → current, keeping a constant offset
 * between the sample point and the brush.
 */
export function cloneSegment(ctx: CanvasRenderingContext2D, source: HTMLCanvasElement, from: { x: number; y: number }, to: { x: number; y: number }, srcFrom: { x: number; y: number }, srcTo: { x: number; y: number }, style: CloneStyle): void {
  const radius = Math.max(1, style.size / 2);
  const dist = Math.hypot(to.x - from.x, to.y - from.y);
  const step = Math.max(1, radius * 0.5);
  const steps = Math.max(1, Math.ceil(dist / step));
  const stampSize = Math.max(2, Math.ceil(radius * 2));
  const stampRadius = stampSize / 2;
  const stamp = makeCanvas(stampSize, stampSize);
  const stampCtx = ctx2d(stamp);
  const hardStop = stampRadius * (1 - Math.max(0, Math.min(1, style.softness)));
  ctx.save();
  ctx.globalAlpha = style.opacity * style.flow;
  for (let i = 1; i <= steps; i++) {
    const t = i / steps;
    const px = from.x + (to.x - from.x) * t;
    const py = from.y + (to.y - from.y) * t;
    const sx = srcFrom.x + (srcTo.x - srcFrom.x) * t;
    const sy = srcFrom.y + (srcTo.y - srcFrom.y) * t;
    stampCtx.clearRect(0, 0, stampSize, stampSize);
    stampCtx.globalCompositeOperation = 'source-over';
    stampCtx.drawImage(source, sx - stampRadius, sy - stampRadius, stampSize, stampSize, 0, 0, stampSize, stampSize);
    stampCtx.globalCompositeOperation = 'destination-in';
    const grad = stampCtx.createRadialGradient(stampRadius, stampRadius, hardStop, stampRadius, stampRadius, stampRadius);
    grad.addColorStop(0, 'rgba(0,0,0,1)');
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    stampCtx.fillStyle = grad;
    stampCtx.fillRect(0, 0, stampSize, stampSize);
    ctx.drawImage(stamp, px - stampRadius, py - stampRadius);
  }
  ctx.restore();
}

/** Brush diameter slider: 0..1000 ↔ 1..800 px on a log scale. */
export function sliderToBrush(v: number): number {
  return Math.max(1, Math.round(Math.pow(800, v / 1000)));
}

export function brushToSlider(px: number): number {
  return Math.round((Math.log(Math.max(1, px)) / Math.log(800)) * 1000);
}
