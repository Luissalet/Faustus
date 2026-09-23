/**
 * The graph view's pure simulation math: one force-simulation step, the
 * fit-to-view transform, screen↔world conversion, zoom-around-a-point and
 * hit testing. Kept apart from `GraphView.tsx` (which owns the canvas, the
 * `requestAnimationFrame` loop and the pointer wiring) so this module can be
 * exercised directly by `studio/checks/brain.check.mjs` the same way
 * `lib/graph.ts`'s deterministic `layout()` is — no DOM, no
 * `requestAnimationFrame`, no randomness beyond the same `hash01` tie-break
 * `layout()` already uses for a degenerate (zero-distance) pair.
 *
 * `lib/graph.ts`'s `layout()` still owns the INITIAL positions (a graph's
 * starting shape has to be deterministic so two renders of the same data
 * agree); everything here takes over from there, frame by frame, and reacts
 * to a drag, a pan, a zoom or new data the way a static layout cannot.
 */

import { hash01, nodeRadius as degreeRadius, type GraphEdge, type Positions, type Point } from '../../lib/graph';

export { degreeRadius as nodeRadius };

/* ── the simulation state ─────────────────────────────────────────── */

export interface SimNode {
  id: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
  /** Set while a person is dragging it: forces still act on its
   *  NEIGHBOURS through it, but nothing moves it except the drag itself. */
  fixed?: boolean;
}

export interface SimState {
  nodes: Map<string, SimNode>;
}

/** Seed a brand new simulation from a set of positions (typically
 *  `lib/graph.ts`'s `layout()` output) — every node starts at rest. */
export function initSim(positions: Positions, ids: string[]): SimState {
  const nodes = new Map<string, SimNode>();
  for (const id of ids) {
    const p = positions[id] ?? { x: 0, y: 0 };
    nodes.set(id, { id, x: p.x, y: p.y, vx: 0, vy: 0 });
  }
  return { nodes };
}

/** Carry a simulation over to a changed node set: a node already in motion
 *  keeps its exact position and velocity (so re-heating after an edit does
 *  not visibly jump), a brand new node seeds from `positions`, and a node
 *  no longer present is simply dropped. */
export function reconcileSim(sim: SimState, positions: Positions, ids: string[]): SimState {
  const nodes = new Map<string, SimNode>();
  for (const id of ids) {
    const existing = sim.nodes.get(id);
    nodes.set(id, existing ?? { id, x: (positions[id] ?? { x: 0, y: 0 }).x, y: (positions[id] ?? { x: 0, y: 0 }).y, vx: 0, vy: 0 });
  }
  return { nodes };
}

export function simPositions(sim: SimState): Positions {
  const out: Positions = {};
  for (const [id, n] of sim.nodes) out[id] = { x: n.x, y: n.y };
  return out;
}

/* ── one force step ──────────────────────────────────────────────── */

export interface ForceOptions {
  width: number;
  height: number;
  /** The spring's rest length along an edge. */
  linkDistance: number;
  /** Coulomb-like repulsion strength between every pair of nodes. */
  repulsion: number;
  linkStrength: number;
  centerStrength: number;
  /** Velocity kept each step, the rest lost to "friction" — this alone is
   *  what makes the system settle instead of oscillating forever. */
  damping: number;
  /** A hard speed cap so a freshly reconciled or reheated layout cannot
   *  fling a node across the canvas in one frame. */
  maxSpeed: number;
  dt: number;
}

export const DEFAULT_FORCE: ForceOptions = {
  width: 800,
  height: 600,
  linkDistance: 70,
  repulsion: 1800,
  linkStrength: 0.06,
  centerStrength: 0.008,
  damping: 0.88,
  maxSpeed: 40,
  dt: 1,
};

/** Kinetic energy stays below this once the layout has visibly settled —
 *  the caller's cue to stop scheduling animation frames. */
export const REST_ENERGY = 0.02;

export function isSettled(energy: number): boolean {
  return energy < REST_ENERGY;
}

/**
 * One tick: pairwise repulsion (O(n²), fine at the `MAX_DRAWN` cap the
 * graph view already enforces), a spring per edge toward `linkDistance`, a
 * weak pull toward the canvas centre, then damping and an integration step.
 * A `fixed` node (mid-drag) still pushes and pulls its neighbours but is
 * never moved by the result. Returns the system's total kinetic energy —
 * the settle/reheat signal — so this stays a pure function of its inputs.
 */
export function stepSimulation(sim: SimState, edges: GraphEdge[], opts: Partial<ForceOptions> = {}): number {
  const o = { ...DEFAULT_FORCE, ...opts };
  const ids = [...sim.nodes.keys()];
  const fx = new Map<string, number>();
  const fy = new Map<string, number>();
  for (const id of ids) {
    fx.set(id, 0);
    fy.set(id, 0);
  }

  for (let i = 0; i < ids.length; i += 1) {
    const a = sim.nodes.get(ids[i])!;
    for (let j = i + 1; j < ids.length; j += 1) {
      const b = sim.nodes.get(ids[j])!;
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      let dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < 0.01) {
        // Two nodes landed on the same point (a brand new pair reconciled
        // at a shared default, say): nudge them apart deterministically
        // rather than dividing by ~0.
        dx = (hash01(`${a.id}:${b.id}`) - 0.5) || 0.05;
        dy = (hash01(`${b.id}:${a.id}`) - 0.5) || 0.05;
        dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      }
      const force = o.repulsion / (dist * dist);
      const fxv = (dx / dist) * force;
      const fyv = (dy / dist) * force;
      fx.set(a.id, fx.get(a.id)! + fxv);
      fy.set(a.id, fy.get(a.id)! + fyv);
      fx.set(b.id, fx.get(b.id)! - fxv);
      fy.set(b.id, fy.get(b.id)! - fyv);
    }
  }

  for (const e of edges) {
    const a = sim.nodes.get(e.from);
    const b = sim.nodes.get(e.to);
    if (!a || !b || a === b) continue;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const force = (dist - o.linkDistance) * o.linkStrength;
    const fxv = (dx / dist) * force;
    const fyv = (dy / dist) * force;
    fx.set(a.id, fx.get(a.id)! + fxv);
    fy.set(a.id, fy.get(a.id)! + fyv);
    fx.set(b.id, fx.get(b.id)! - fxv);
    fy.set(b.id, fy.get(b.id)! - fyv);
  }

  const cx = o.width / 2;
  const cy = o.height / 2;
  let energy = 0;
  for (const id of ids) {
    const n = sim.nodes.get(id)!;
    if (n.fixed) {
      n.vx = 0;
      n.vy = 0;
      continue;
    }
    const ax = fx.get(id)! + (cx - n.x) * o.centerStrength;
    const ay = fy.get(id)! + (cy - n.y) * o.centerStrength;
    n.vx = (n.vx + ax * o.dt) * o.damping;
    n.vy = (n.vy + ay * o.dt) * o.damping;
    const speed = Math.hypot(n.vx, n.vy);
    if (speed > o.maxSpeed) {
      n.vx = (n.vx / speed) * o.maxSpeed;
      n.vy = (n.vy / speed) * o.maxSpeed;
    }
    n.x += n.vx * o.dt;
    n.y += n.vy * o.dt;
    energy += n.vx * n.vx + n.vy * n.vy;
  }
  return energy;
}

/* ── viewport transform: pan + zoom ─────────────────────────────────── */

export interface Transform {
  scale: number;
  tx: number;
  ty: number;
}

export const ZOOM_MIN = 0.15;
export const ZOOM_MAX = 6;

export function clampScale(scale: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, scale));
}

export function worldToScreen(t: Transform, p: Point): Point {
  return { x: p.x * t.scale + t.tx, y: p.y * t.scale + t.ty };
}

export function screenToWorld(t: Transform, p: Point): Point {
  return { x: (p.x - t.tx) / t.scale, y: (p.y - t.ty) / t.scale };
}

export function panBy(t: Transform, dx: number, dy: number): Transform {
  return { ...t, tx: t.tx + dx, ty: t.ty + dy };
}

/**
 * Zoom by `factor` (>1 zooms in, <1 zooms out), clamped to
 * `[ZOOM_MIN, ZOOM_MAX]`, while keeping the WORLD point currently under
 * `screenPoint` fixed on screen — the usual "zoom toward the cursor" feel a
 * wheel handler wants. Solved directly from `screenToWorld` staying equal
 * before and after, so clamping the scale never breaks the fixed point.
 */
export function zoomAround(t: Transform, screenPoint: Point, factor: number): Transform {
  const nextScale = clampScale(t.scale * factor);
  const applied = nextScale / t.scale;
  return {
    scale: nextScale,
    tx: screenPoint.x - (screenPoint.x - t.tx) * applied,
    ty: screenPoint.y - (screenPoint.y - t.ty) * applied,
  };
}

/**
 * The transform that fits every listed node inside `viewport`, centred,
 * with `pad` screen pixels of margin — the graph's starting view and what
 * a double-click on the background restores. A single node (zero-size
 * bounding box) still gets a sane, clamped scale rather than zooming to
 * infinity.
 */
export function fitTransform(positions: Positions, ids: string[], viewport: { width: number; height: number }, pad = 40): Transform {
  const width = Math.max(viewport.width, 1);
  const height = Math.max(viewport.height, 1);
  if (!ids.length) return { scale: 1, tx: width / 2, ty: height / 2 };
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const id of ids) {
    const p = positions[id];
    if (!p) continue;
    minX = Math.min(minX, p.x);
    maxX = Math.max(maxX, p.x);
    minY = Math.min(minY, p.y);
    maxY = Math.max(maxY, p.y);
  }
  if (!Number.isFinite(minX)) return { scale: 1, tx: width / 2, ty: height / 2 };
  const spanX = Math.max(maxX - minX, 1);
  const spanY = Math.max(maxY - minY, 1);
  const usableX = Math.max(width - pad * 2, 1);
  const usableY = Math.max(height - pad * 2, 1);
  const scale = clampScale(Math.min(usableX / spanX, usableY / spanY));
  const worldCx = (minX + maxX) / 2;
  const worldCy = (minY + maxY) / 2;
  return { scale, tx: width / 2 - worldCx * scale, ty: height / 2 - worldCy * scale };
}

/* ── hit testing ──────────────────────────────────────────────────── */

/**
 * The topmost node under a screen point, or `null`. Distance is compared
 * in WORLD units (the click is converted once via `screenToWorld`), so the
 * hit radius a caller passes is the same node radius used to draw it,
 * regardless of the current zoom — `radiusFor` gets the node's `id`, kept
 * generic rather than importing `lib/graph.ts`'s degree map so a caller
 * with a different sizing rule can reuse this unchanged.
 */
export function hitTest(ids: string[], positions: Positions, radiusFor: (id: string) => number, transform: Transform, screenPoint: Point): string | null {
  const world = screenToWorld(transform, screenPoint);
  const pad = 3 / transform.scale;
  let best: string | null = null;
  let bestDist = Infinity;
  for (const id of ids) {
    const p = positions[id];
    if (!p) continue;
    const r = radiusFor(id) + pad;
    const d = Math.hypot(p.x - world.x, p.y - world.y);
    if (d <= r && d < bestDist) {
      bestDist = d;
      best = id;
    }
  }
  return best;
}

/** A node's immediate neighbours by edge — what hover highlighting dims
 *  everything else against. */
export function neighborsOf(id: string, edges: GraphEdge[]): Set<string> {
  const out = new Set<string>();
  for (const e of edges) {
    if (e.from === id) out.add(e.to);
    else if (e.to === id) out.add(e.from);
  }
  return out;
}
