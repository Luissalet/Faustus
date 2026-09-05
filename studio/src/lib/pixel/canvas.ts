/**
 * Canvas primitives shared by the pixel editor. Pure: every function takes
 * what it needs and returns a fresh canvas or a value; none of them touch
 * module state or the DOM beyond creating canvases.
 *
 * Ported from the legacy `static/js/editor/{canvas-coords,checkerboard}.js`.
 */

export function makeCanvas(width: number, height: number): HTMLCanvasElement {
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(width));
  c.height = Math.max(1, Math.round(height));
  return c;
}

export function ctx2d(canvas: HTMLCanvasElement): CanvasRenderingContext2D {
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) throw new Error('2D canvas is not available');
  return ctx;
}

export function cloneCanvas(src: HTMLCanvasElement): HTMLCanvasElement {
  const c = makeCanvas(src.width, src.height);
  ctx2d(c).drawImage(src, 0, 0);
  return c;
}

/** True when at least one pixel has alpha > 0 (sampled every `step` px). */
export function hasPixels(canvas: HTMLCanvasElement, step = 1): boolean {
  const { width, height } = canvas;
  if (!width || !height) return false;
  const d = ctx2d(canvas).getImageData(0, 0, width, height).data;
  const stride = Math.max(1, step);
  for (let y = 0; y < height; y += stride) {
    for (let x = 0; x < width; x += stride) {
      if (d[(y * width + x) * 4 + 3] > 0) return true;
    }
  }
  return false;
}

/** Bounding box of the opaque pixels, or null when the canvas is empty. */
export function alphaBounds(canvas: HTMLCanvasElement, step = 2): { x: number; y: number; w: number; h: number } | null {
  const { width, height } = canvas;
  const d = ctx2d(canvas).getImageData(0, 0, width, height).data;
  let minX = width, maxX = -1, minY = height, maxY = -1;
  for (let y = 0; y < height; y += step) {
    for (let x = 0; x < width; x += step) {
      if (d[(y * width + x) * 4 + 3] > 0) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  if (maxX < 0) return null;
  return { x: minX, y: minY, w: maxX - minX + 1, h: maxY - minY + 1 };
}

/**
 * Transparency checkerboard beneath the document so empty areas read as
 * empty. Colours are deliberately neutral greys (they are pixels of the
 * picture surface, not UI chrome, so they do not follow the theme).
 */
export function drawCheckerboard(ctx: CanvasRenderingContext2D, w: number, h: number, size = 10): void {
  ctx.fillStyle = '#c8c8c8';
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = '#f2f2f2';
  for (let y = 0; y < h; y += size) {
    for (let x = 0; x < w; x += size) {
      if ((Math.floor(x / size) + Math.floor(y / size)) % 2 === 0) ctx.fillRect(x, y, size, size);
    }
  }
}

/** Client coordinates → document pixel coordinates for a displayed canvas. */
export function canvasCoords(clientX: number, clientY: number, canvas: HTMLCanvasElement): { x: number; y: number } {
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / Math.max(1, rect.width);
  const scaleY = canvas.height / Math.max(1, rect.height);
  return { x: (clientX - rect.left) * scaleX, y: (clientY - rect.top) * scaleY };
}

export function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('The image could not be decoded'));
    img.src = src;
  });
}

export function imageToCanvas(img: CanvasImageSource, width: number, height: number): HTMLCanvasElement {
  const c = makeCanvas(width, height);
  const ctx = ctx2d(c);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(img, 0, 0, width, height);
  return c;
}

export function toBase64Png(canvas: HTMLCanvasElement): string {
  return canvas.toDataURL('image/png').split(',')[1] ?? '';
}

export function canvasToBlob(canvas: HTMLCanvasElement, mime = 'image/png', quality?: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('Canvas encode failed'))), mime, quality);
  });
}

export function scaleCanvas(src: HTMLCanvasElement, width: number, height: number): HTMLCanvasElement {
  return imageToCanvas(src, width, height);
}

/** Resize a canvas in place keeping its pixels (top-left anchored). */
export function resizeInPlace(canvas: HTMLCanvasElement, width: number, height: number, dx = 0, dy = 0): void {
  const copy = cloneCanvas(canvas);
  canvas.width = Math.max(1, Math.round(width));
  canvas.height = Math.max(1, Math.round(height));
  ctx2d(canvas).drawImage(copy, dx, dy);
}
