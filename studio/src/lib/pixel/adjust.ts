/**
 * Adjustment layers: brightness/contrast, hue/saturation, levels, colour
 * balance, and the luminance histogram. Pure pixel maths ported from the
 * legacy `fx/pixel-pass.js` and `fx/histogram.js`.
 */
import { ctx2d, makeCanvas } from './canvas';

export type AdjustmentType = 'brightness-contrast' | 'hue-saturation' | 'levels' | 'color-balance';

export interface Rgb {
  r: number;
  g: number;
  b: number;
}

export type AdjustmentParams =
  | { brightness: number; contrast: number }
  | { hue: number; saturation: number }
  | { inBlack: number; inWhite: number; gamma: number; outBlack: number; outWhite: number }
  | { shadows: Rgb; midtones: Rgb; highlights: Rgb };

export interface Adjustment {
  type: AdjustmentType;
  params: Record<string, unknown>;
}

export interface AdjustmentLayer extends Adjustment {
  id: string;
  name: string;
  visible: boolean;
  opacity: number;
}

export function defaultParams(type: AdjustmentType): Record<string, unknown> {
  switch (type) {
    case 'brightness-contrast':
      return { brightness: 1, contrast: 1 };
    case 'hue-saturation':
      return { hue: 0, saturation: 1 };
    case 'levels':
      return { inBlack: 0, inWhite: 255, gamma: 1, outBlack: 0, outWhite: 255 };
    case 'color-balance':
      return { shadows: { r: 0, g: 0, b: 0 }, midtones: { r: 0, g: 0, b: 0 }, highlights: { r: 0, g: 0, b: 0 } };
  }
}

function levelsLut(l: { inBlack: number; inWhite: number; gamma: number; outBlack: number; outWhite: number }): Uint8ClampedArray {
  const inLow = Math.max(0, Math.min(254, l.inBlack));
  const inHigh = Math.max(inLow + 1, Math.min(255, l.inWhite));
  const gamma = Math.max(0.1, l.gamma || 1);
  const outLow = Math.max(0, Math.min(255, l.outBlack));
  const outHigh = Math.max(outLow, Math.min(255, l.outWhite));
  const inv = 1 / gamma;
  const span = outHigh - outLow;
  const lut = new Uint8ClampedArray(256);
  for (let v = 0; v < 256; v++) {
    let x = (v - inLow) / (inHigh - inLow);
    if (x < 0) x = 0;
    else if (x > 1) x = 1;
    x = Math.pow(x, inv);
    lut[v] = Math.round(x * span + outLow);
  }
  return lut;
}

/** Apply one adjustment to a canvas and return the result as a new canvas. */
export function applyAdjustment(src: HTMLCanvasElement, adj: Adjustment): HTMLCanvasElement {
  const w = src.width, h = src.height;
  const out = makeCanvas(w, h);
  const octx = ctx2d(out);
  const p = adj.params as Record<string, number>;
  if (adj.type === 'brightness-contrast') {
    octx.filter = `brightness(${p.brightness}) contrast(${p.contrast})`;
    octx.drawImage(src, 0, 0);
    octx.filter = 'none';
    return out;
  }
  if (adj.type === 'hue-saturation') {
    octx.filter = `saturate(${p.saturation}) hue-rotate(${p.hue}deg)`;
    octx.drawImage(src, 0, 0);
    octx.filter = 'none';
    return out;
  }
  octx.drawImage(src, 0, 0);
  const img = octx.getImageData(0, 0, w, h);
  const d = img.data;
  if (adj.type === 'levels') {
    const lut = levelsLut(adj.params as never);
    for (let i = 0; i < d.length; i += 4) {
      d[i] = lut[d[i]];
      d[i + 1] = lut[d[i + 1]];
      d[i + 2] = lut[d[i + 2]];
    }
    octx.putImageData(img, 0, 0);
    return out;
  }
  if (adj.type === 'color-balance') {
    const cb = adj.params as { shadows: Rgb; midtones: Rgb; highlights: Rgb };
    const scale = 0.6;
    const s = cb.shadows, m = cb.midtones, hi = cb.highlights;
    const sR = s.r * scale, sG = s.g * scale, sB = s.b * scale;
    const mR = m.r * scale, mG = m.g * scale, mB = m.b * scale;
    const hR = hi.r * scale, hG = hi.g * scale, hB = hi.b * scale;
    const wS = new Float32Array(256), wM = new Float32Array(256), wH = new Float32Array(256);
    const sig = 0.25;
    for (let v = 0; v < 256; v++) {
      const x = v / 255;
      wS[v] = Math.exp(-(x * x) / (2 * sig * sig));
      wM[v] = Math.exp(-((x - 0.5) * (x - 0.5)) / (2 * sig * sig));
      wH[v] = Math.exp(-((1 - x) * (1 - x)) / (2 * sig * sig));
    }
    for (let i = 0; i < d.length; i += 4) {
      let r = d[i], g = d[i + 1], b = d[i + 2];
      const Y = (0.2126 * r + 0.7152 * g + 0.0722 * b) | 0;
      const ws = wS[Y], wm = wM[Y], wh = wH[Y];
      r += sR * ws + mR * wm + hR * wh;
      g += sG * ws + mG * wm + hG * wh;
      b += sB * ws + mB * wm + hB * wh;
      d[i] = r < 0 ? 0 : r > 255 ? 255 : r;
      d[i + 1] = g < 0 ? 0 : g > 255 ? 255 : g;
      d[i + 2] = b < 0 ? 0 : b > 255 ? 255 : b;
    }
    octx.putImageData(img, 0, 0);
    return out;
  }
  return out;
}

/**
 * Run a stack of adjustment layers (plus an optional staged preview) over a
 * canvas. Returns the source itself when there is nothing to apply.
 */
export function renderAdjustmentStack(src: HTMLCanvasElement, stack: AdjustmentLayer[], staged: Adjustment | null, skipId: string | null): HTMLCanvasElement {
  const live = stack.filter((a) => a.visible && a.id !== skipId);
  if (!live.length && !staged) return src;
  let cur = src;
  const w = src.width, h = src.height;
  for (const adj of live) {
    const adjOut = applyAdjustment(cur, adj);
    if (adj.opacity >= 0.999) {
      cur = adjOut;
    } else {
      const blend = makeCanvas(w, h);
      const bctx = ctx2d(blend);
      bctx.drawImage(cur, 0, 0);
      bctx.globalAlpha = adj.opacity;
      bctx.drawImage(adjOut, 0, 0);
      bctx.globalAlpha = 1;
      cur = blend;
    }
  }
  if (staged) cur = applyAdjustment(cur, staged);
  return cur;
}

export function stackSignature(stack: AdjustmentLayer[], staged: Adjustment | null, skipId: string | null): string {
  return (
    stack.map((a) => `${a.id}:${a.visible ? 1 : 0}:${a.opacity}:${a.type}:${JSON.stringify(a.params)}`).join('|') +
    (staged ? `|S:${staged.type}:${JSON.stringify(staged.params)}` : '') +
    (skipId ? `|E:${skipId}` : '')
  );
}

/** Rec. 709 luminance histogram (256 bins), sampled at ≤ 400×400. */
export function luminanceHistogram(src: HTMLCanvasElement): Uint32Array {
  const sampleW = Math.min(400, src.width), sampleH = Math.min(400, src.height);
  const tmp = makeCanvas(sampleW, sampleH);
  const tctx = ctx2d(tmp);
  tctx.drawImage(src, 0, 0, sampleW, sampleH);
  const img = tctx.getImageData(0, 0, sampleW, sampleH).data;
  const hist = new Uint32Array(256);
  for (let i = 0; i < img.length; i += 4) {
    if (img[i + 3] < 8) continue;
    const Y = (0.2126 * img[i] + 0.7152 * img[i + 1] + 0.0722 * img[i + 2]) | 0;
    hist[Math.min(255, Y)]++;
  }
  return hist;
}

/**
 * Paint a histogram into a canvas. Colours come from the caller (resolved
 * from the theme's tokens) so the picture follows light/dark.
 */
export function drawHistogram(canvas: HTMLCanvasElement, hist: Uint32Array, colors: { bar: string; black?: string; white?: string }, markers?: { inBlack: number; inWhite: number }): void {
  const w = canvas.width, h = canvas.height;
  const ctx = ctx2d(canvas);
  ctx.clearRect(0, 0, w, h);
  let peak = 1;
  for (let i = 0; i < 256; i++) if (hist[i] > peak) peak = hist[i];
  ctx.fillStyle = colors.bar;
  for (let i = 0; i < 256; i++) {
    const x = (i / 256) * w;
    const bh = Math.pow(hist[i] / peak, 0.5) * h;
    ctx.fillRect(x, h - bh, w / 256 + 0.5, bh);
  }
  if (markers) {
    ctx.fillStyle = colors.black ?? colors.bar;
    ctx.fillRect((markers.inBlack / 256) * w, 0, 1, h);
    ctx.fillStyle = colors.white ?? colors.bar;
    ctx.fillRect((markers.inWhite / 256) * w, 0, 1, h);
  }
}

export function adjustmentLabel(type: AdjustmentType): string {
  switch (type) {
    case 'brightness-contrast':
      return 'Brightness / Contrast';
    case 'hue-saturation':
      return 'Hue / Saturation';
    case 'levels':
      return 'Levels';
    case 'color-balance':
      return 'Color balance';
  }
}
