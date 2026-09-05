/**
 * Snap-while-dragging for the move tool, plus the cursor for each transform
 * handle. Ported from the legacy `snap.js`.
 */

export interface SnapLayer {
  id: string;
  visible: boolean;
  width: number;
  height: number;
  offset: { x: number; y: number };
}

export interface SnapGuide {
  vertical: boolean;
  at: number;
}

export function computeSnap(moving: { id: string; width: number; height: number }, nx: number, ny: number, ctx: { zoom: number; canvasW: number; canvasH: number; others: SnapLayer[] }): { x: number; y: number; guides: SnapGuide[] } {
  const zoom = Number.isFinite(ctx.zoom) ? ctx.zoom : 1;
  const SNAP_PX = 6 / Math.max(zoom, 0.0001);
  const cw = ctx.canvasW || 0, ch = ctx.canvasH || 0;
  const w = moving.width, h = moving.height;
  const vTargets = [0, cw, cw / 2];
  const hTargets = [0, ch, ch / 2];
  for (const other of ctx.others) {
    if (!other.visible || other.id === moving.id) continue;
    const o = other.offset;
    vTargets.push(o.x, o.x + other.width, o.x + other.width / 2);
    hTargets.push(o.y, o.y + other.height, o.y + other.height / 2);
  }
  const myX: Record<string, number> = { l: nx, cx: nx + w / 2, r: nx + w };
  const myY: Record<string, number> = { t: ny, cy: ny + h / 2, b: ny + h };
  let bestX: { to: number; src: string } | null = null, bestDx = Infinity;
  let bestY: { to: number; src: string } | null = null, bestDy = Infinity;
  for (const [src, val] of Object.entries(myX)) {
    for (const tx of vTargets) {
      const d = Math.abs(tx - val);
      if (d < SNAP_PX && d < bestDx) {
        bestDx = d;
        bestX = { to: tx, src };
      }
    }
  }
  for (const [src, val] of Object.entries(myY)) {
    for (const ty of hTargets) {
      const d = Math.abs(ty - val);
      if (d < SNAP_PX && d < bestDy) {
        bestDy = d;
        bestY = { to: ty, src };
      }
    }
  }
  const guides: SnapGuide[] = [];
  let sx = nx, sy = ny;
  if (bestX) {
    sx = bestX.src === 'l' ? bestX.to : bestX.src === 'cx' ? bestX.to - w / 2 : bestX.to - w;
    guides.push({ vertical: true, at: bestX.to });
  }
  if (bestY) {
    sy = bestY.src === 't' ? bestY.to : bestY.src === 'cy' ? bestY.to - h / 2 : bestY.to - h;
    guides.push({ vertical: false, at: bestY.to });
  }
  return { x: sx, y: sy, guides };
}

export type HandleId = 'tl' | 'tr' | 'bl' | 'br' | 'rot';

export function cursorForHandle(id: HandleId | null): string {
  switch (id) {
    case 'tl':
    case 'br':
      return 'nwse-resize';
    case 'tr':
    case 'bl':
      return 'nesw-resize';
    case 'rot':
      return 'grab';
    default:
      return 'default';
  }
}
