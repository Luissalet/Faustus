'use strict';
// The state mark drawn over the tray icon: a filled circle in the bottom-right
// corner (blue with the number of turns running, amber "?" when one waits for
// the owner, red "!" when one failed, green tick when one finished unseen) and
// an optional small dot in the top-right corner for what the main mark hides.
//
// Works on raw 4-bytes-per-pixel bitmaps (the layout nativeImage.toBitmap()
// hands out on Windows and Linux: B, G, R, A, premultiplied), so the drawing
// is checked by `node --test tray-badge.test.cjs` without Electron.

const COLORS = Object.freeze({
  running: [45, 125, 246], waiting: [240, 150, 10], failed: [222, 48, 48], done: [30, 170, 90], offline: [128, 134, 144],
});
const RIM = [18, 20, 24];   // the app's own dark, so the mark reads on a light or a dark taskbar
const INK = [255, 255, 255];

const GLYPHS = Object.freeze({
  0: ['###', '#.#', '#.#', '#.#', '###'], 1: ['.#.', '##.', '.#.', '.#.', '###'], 2: ['###', '..#', '###', '#..', '###'],
  3: ['###', '..#', '###', '..#', '###'], 4: ['#.#', '#.#', '###', '..#', '..#'], 5: ['###', '#..', '###', '..#', '###'],
  6: ['###', '#..', '###', '#.#', '###'], 7: ['###', '..#', '..#', '.#.', '.#.'], 8: ['###', '#.#', '###', '#.#', '###'],
  9: ['###', '#.#', '###', '..#', '###'], '+': ['...', '.#.', '###', '.#.', '...'], '?': ['###', '..#', '.#.', '...', '.#.'],
  '!': ['.#.', '.#.', '.#.', '...', '.#.'], tick: ['....#', '...#.', '#.#..', '.#...', '.....'], '-': ['...', '...', '###', '...', '...'],
});

/** What the main mark says for a summary from turn-status.cjs (`link`: '' | 'offline' | 'signed-out'). */
function markFor(summary, link) {
  if (link === 'offline' || link === 'signed-out') return {kind: 'offline', glyph: '-', corner: ''};
  const s = summary || {kind: 'idle'};
  if (s.kind === 'idle') return null;
  const glyph = s.kind === 'running' ? (s.count > 9 ? '+' : String(Math.max(1, s.count))) : s.kind === 'waiting' ? '?' : s.kind === 'failed' ? '!' : 'tick';
  return {kind: s.kind, glyph, corner: s.corner || ''};
}

function blend(buffer, size, x, y, rgb, alpha) {
  if (x < 0 || y < 0 || x >= size || y >= size || alpha <= 0) return;
  const i = (y * size + x) * 4, a = Math.min(1, alpha), keep = 1 - a;
  buffer[i] = Math.round(rgb[2] * a + buffer[i] * keep);
  buffer[i + 1] = Math.round(rgb[1] * a + buffer[i + 1] * keep);
  buffer[i + 2] = Math.round(rgb[0] * a + buffer[i + 2] * keep);
  buffer[i + 3] = Math.round(255 * a + buffer[i + 3] * keep);
}

function disc(buffer, size, cx, cy, radius, rgb) {
  for (let y = Math.floor(cy - radius - 1); y <= Math.ceil(cy + radius + 1); y++) {
    for (let x = Math.floor(cx - radius - 1); x <= Math.ceil(cx + radius + 1); x++) {
      const distance = Math.hypot(x + 0.5 - cx, y + 0.5 - cy);
      blend(buffer, size, x, y, rgb, Math.max(0, Math.min(1, radius - distance + 0.5)));   // one pixel of soft edge
    }
  }
}

function glyph(buffer, size, cx, cy, name, scale) {
  const rows = GLYPHS[name];
  if (!rows) return;
  const width = rows[0].length * scale, height = rows.length * scale;
  const left = Math.round(cx - width / 2), top = Math.round(cy - height / 2);
  rows.forEach((row, r) => [...row].forEach((cell, c) => {
    if (cell !== '#') return;
    for (let dy = 0; dy < scale; dy++) for (let dx = 0; dx < scale; dx++) blend(buffer, size, left + c * scale + dx, top + r * scale + dy, INK, 1);
  }));
}

/** A copy of the `size`×`size` bitmap with the mark drawn on it (the same bitmap back when there is nothing to say). */
function paint(bitmap, size, mark) {
  const out = Buffer.from(bitmap);
  if (!mark) return out;
  const radius = Math.max(4, Math.round(size * 0.31)), cx = size - radius - 0.5, cy = size - radius - 0.5;
  disc(out, size, cx, cy, radius + Math.max(1, size / 16), RIM);
  disc(out, size, cx, cy, radius, COLORS[mark.kind] || COLORS.offline);
  glyph(out, size, cx, cy, mark.glyph, Math.max(1, Math.floor((radius * 2) / 7)));
  if (mark.corner && COLORS[mark.corner]) {
    const r = Math.max(2, Math.round(size * 0.15)), x = size - r - 0.5, y = r + 0.5;
    disc(out, size, x, y, r + Math.max(1, size / 20), RIM);
    disc(out, size, x, y, r, COLORS[mark.corner]);
  }
  return out;
}

/** The mark alone on a transparent square: the overlay Windows draws on the taskbar button. */
function alone(size, mark) {
  const out = Buffer.alloc(size * size * 4);
  if (!mark) return out;
  const radius = size / 2 - 1, c = size / 2;
  disc(out, size, c, c, radius, COLORS[mark.kind] || COLORS.offline);
  glyph(out, size, c, c, mark.glyph, Math.max(1, Math.floor((radius * 2) / 7)));
  return out;
}

module.exports = {COLORS, GLYPHS, markFor, paint, alone};
