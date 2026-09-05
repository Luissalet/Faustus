/**
 * The drawing surface of a note.
 *
 * A sticky note is not a paint program, so this is deliberately small: a
 * pen, an eraser, a straight line, a circle and some text, on a 600×320
 * canvas that may have a photo underneath. Studio already has a real image
 * editor (`/library/edit`); this is the other thing, the one you use with a
 * finger while the kettle boils.
 *
 * The pixel literals live here because a canvas draws pixels, not design
 * tokens, and a `.ts` is where the guard (tests/test_studio_guards.py)
 * expects them. Everything in this file is pure: the component
 * (`screens/notes/Draw.tsx`) does the DOM.
 */

export const CANVAS_W = 600;
export const CANVAS_H = 320;

export type Tool = 'pen' | 'eraser' | 'line' | 'circle' | 'text';

/** The ink. Eight that read on white, dark first because most strokes are. */
export const INKS = ['#1c1c1c', '#d23b3b', '#e07a1f', '#e0b91f', '#3fa34d', '#2f6fd0', '#7b4fc0', '#ffffff'] as const;

/** What a stroke, a shape or a letter is: three sizes, no slider. */
export const WIDTHS = { s: 2, m: 5, l: 10 } as const;
export const TEXT_SIZES = { s: 16, m: 26, l: 40 } as const;
export type Size = keyof typeof WIDTHS;

export const PAPER = '#ffffff';

/** How many bitmaps the undo stack keeps. Bounded on purpose. */
export const UNDO_LIMIT = 24;

export interface Point {
  x: number;
  y: number;
}

/**
 * A pointer event's position in canvas coordinates.
 *
 * The canvas is drawn at its logical size but displayed at whatever width
 * the dialog gives it, so the ratio has to come from the rendered box. Get
 * this wrong and the ink lands where the finger was not.
 */
export function pointIn(rect: { left: number; top: number; width: number; height: number }, clientX: number, clientY: number, logical = { width: CANVAS_W, height: CANVAS_H }): Point {
  const scaleX = rect.width > 0 ? logical.width / rect.width : 1;
  const scaleY = rect.height > 0 ? logical.height / rect.height : 1;
  return { x: (clientX - rect.left) * scaleX, y: (clientY - rect.top) * scaleY };
}

/** The radius of a circle dragged from `from` to `to`. */
export function radius(from: Point, to: Point): number {
  return Math.hypot(to.x - from.x, to.y - from.y);
}

/** True when a drag is long enough to be a shape and not a slip. */
export function isDrag(from: Point, to: Point): boolean {
  return Math.abs(to.x - from.x) + Math.abs(to.y - from.y) > 2;
}

/** Add a snapshot to a bounded undo stack, oldest out first. */
export function pushUndo<T>(stack: T[], snapshot: T, limit = UNDO_LIMIT): T[] {
  const next = [...stack, snapshot];
  return next.length > limit ? next.slice(next.length - limit) : next;
}

/** A blank note drawing is not worth uploading. */
export function isBlank(pixels: Uint8ClampedArray): boolean {
  for (let i = 0; i < pixels.length; i += 4) {
    if (pixels[i] !== 255 || pixels[i + 1] !== 255 || pixels[i + 2] !== 255) return false;
  }
  return true;
}

/** `bg:<url>` is how a note stores a background image in its colour field. */
export const BG_PREFIX = 'bg:';

export function backgroundOf(color: string): string | null {
  return color.startsWith(BG_PREFIX) ? color.slice(BG_PREFIX.length) : null;
}

export function asBackground(url: string): string {
  return `${BG_PREFIX}${url}`;
}

/** Only same-origin uploads and data URLs get to be a note's picture. */
export function safeImage(url: string | null | undefined): string | null {
  const value = (url ?? '').trim();
  if (!value) return null;
  if (value.startsWith('data:image/')) return value;
  if (value.startsWith('/')) return value;
  if (/^https?:\/\//i.test(value)) return value;
  return null;
}
