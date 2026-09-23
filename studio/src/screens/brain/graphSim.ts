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

import { hash01, type GraphEdge, type Positions, type Point } from '../../lib/graph';

export { hash01 };

/**
 * The Brain graph draws far denser neighbourhoods than the provenance graph
 * `lib/graph.ts`'s own `nodeRadius` was tuned for (a vault's Home note alone
 * can touch every project), so this screen keeps its own, smaller range: a
 * disconnected note reads as a small dot rather than competing visually with
 * a hub, and even a very well-connected note never grows past a size that
 * would swallow its neighbours at zoom 1.
 */
export function nodeRadius(degree: number | undefined): number {
  const d = Math.max(0, degree ?? 0);
  return Math.round((4 + Math.min(10, Math.sqrt(d) * 3)) * 10) / 10;
}

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
  /** Extra screen space kept between two nodes' circles on top of their
   *  radii — the collision pass never lets them settle edge-to-edge. */
  collisionPadding: number;
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
  collisionPadding: 2,
};

/** Kinetic energy stays below this once the layout has visibly settled —
 *  the caller's cue to stop scheduling animation frames. Collision overlap
 *  (see `resolveCollisions`) is folded into the same figure, so a dense
 *  cluster that is still untangling never reads as "settled" early. */
export const REST_ENERGY = 0.02;
/** Below this many screen pixels of leftover overlap, `resolveCollisions`
 *  stops counting it as unsettled — otherwise floating-point remainders
 *  from the position-based correction would keep the simulation "hot"
 *  forever, chasing an overlap too small to ever see. */
const COLLISION_REST_PX = 0.25;

export function isSettled(energy: number): boolean {
  return energy < REST_ENERGY;
}

/**
 * Pushes every overlapping pair of circles apart just enough to clear
 * `collisionPadding`, Gauss–Seidel style (each pair correction sees the
 * PREVIOUS pair's already-moved positions within the same pass) — the same
 * family of technique as d3-force's collide, chosen over a spring-like force
 * because a spring only ever approaches zero overlap asymptotically, never
 * reaching it, and "no node overlap" has to hold exactly once the layout is
 * declared settled. A `fixed` node (mid-drag) absorbs none of the
 * correction itself; its neighbour gets pushed clear of it instead. Returns
 * the largest single-pair overlap still outstanding, in screen pixels, so
 * the caller can fold "still untangling a dense cluster" into the same
 * energy figure that governs `isSettled`.
 */
export function resolveCollisions(sim: SimState, ids: string[], radiusFor: (id: string) => number, padding: number): number {
  let worst = 0;
  for (let i = 0; i < ids.length; i += 1) {
    const a = sim.nodes.get(ids[i]);
    if (!a) continue;
    const ra = radiusFor(ids[i]);
    for (let j = i + 1; j < ids.length; j += 1) {
      const b = sim.nodes.get(ids[j]);
      if (!b) continue;
      const rb = radiusFor(ids[j]);
      const minDist = ra + rb + padding;
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      let dist = Math.sqrt(dx * dx + dy * dy);
      if (dist >= minDist) continue;
      if (dist < 0.01) {
        dx = (hash01(`${a.id}:${b.id}:collide`) - 0.5) || 0.05;
        dy = (hash01(`${b.id}:${a.id}:collide`) - 0.5) || 0.05;
        dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      }
      const overlap = minDist - dist;
      worst = Math.max(worst, overlap);
      const ux = (dx / dist) * overlap;
      const uy = (dy / dist) * overlap;
      if (a.fixed && b.fixed) continue;
      if (a.fixed) {
        b.x += ux;
        b.y += uy;
      } else if (b.fixed) {
        a.x -= ux;
        a.y -= uy;
      } else {
        a.x -= ux / 2;
        a.y -= uy / 2;
        b.x += ux / 2;
        b.y += uy / 2;
      }
    }
  }
  return worst;
}

/**
 * One tick: pairwise repulsion (O(n²), fine at the `MAX_DRAWN` cap the
 * graph view already enforces), a spring per edge toward `linkDistance`, a
 * weak pull toward the canvas centre, damping and an integration step, then
 * a `resolveCollisions` pass so two circles never end up on top of each
 * other. A `fixed` node (mid-drag) still pushes and pulls its neighbours but
 * is never moved by the result. Returns the system's total kinetic energy —
 * the settle/reheat signal — so this stays a pure function of its inputs.
 * `radiusFor` is optional so a caller that does not care about overlap
 * (an existing test fixture, say) can skip the collision pass entirely.
 */
export function stepSimulation(sim: SimState, edges: GraphEdge[], opts: Partial<ForceOptions> = {}, radiusFor?: (id: string) => number): number {
  const o = { ...DEFAULT_FORCE, ...opts };
  const ids = [...sim.nodes.keys()];
  const fx = new Map<string, number>();
  const fy = new Map<string, number>();
  for (const id of ids) {
    fx.set(id, 0);
    fy.set(id, 0);
  }

  // A bigger, busier neighbourhood pushes harder apart than a sparse one —
  // without this, a cluster with dozens of mutually-repelling nodes settles
  // into the same tight ball a five-node graph would, just because each
  // individual repulsion term is unchanged; scaling by how many nodes are
  // sharing this simulation spreads a dense cluster out visibly more.
  const densityBoost = 1 + Math.min(1.5, Math.max(0, ids.length - 1) / 40);
  const repulsion = o.repulsion * densityBoost;

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
      const force = repulsion / (dist * dist);
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
  if (radiusFor) {
    const overlap = resolveCollisions(sim, ids, radiusFor, o.collisionPadding);
    if (overlap > COLLISION_REST_PX) energy = Math.max(energy, REST_ENERGY * 2);
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

/* ── label decluttering ──────────────────────────────────────────────
 * The canvas draws a label as text starting just past the node's edge, at a
 * roughly-constant SCREEN size regardless of zoom (the draw loop shrinks the
 * font by `1 / transform.scale` before scaling the whole canvas back up by
 * `transform.scale`) — so a label's on-screen footprint is cheap to estimate
 * without ever touching a `CanvasRenderingContext2D` (no DOM, no font
 * metrics, stays a pure function this checks file can call directly). */

export interface LabelBox {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

/** A rough but stable estimate of a label's on-screen bounding box: the
 *  monospace-ish average glyph width the draw loop's small sans-serif font
 *  renders at, positioned exactly where `GraphView`'s draw loop puts the
 *  text — just past the node's (zoomed) radius, vertically centred on it. */
export function labelBox(id: string, world: Point, radiusWorld: number, label: string, transform: Transform, charWidth = 5.6, lineHeight = 13): LabelBox {
  const screen = worldToScreen(transform, world);
  const gap = radiusWorld * transform.scale + 4;
  return {
    id,
    x: screen.x + gap,
    y: screen.y - lineHeight / 2,
    width: Math.max(charWidth, label.length * charWidth),
    height: lineHeight,
  };
}

function boxesOverlap(a: LabelBox, b: LabelBox): boolean {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}

/**
 * Greedily keeps a label only when it does not overlap a higher-priority
 * label already kept — the graph's answer to "don't draw a label that would
 * overlap an already-drawn one". `boxes` should already be given in priority
 * order (busiest/most relevant node first); `alwaysShow` (typically the
 * hovered node, the selected/centre note, and their neighbours) is moved to
 * the front regardless of the order `boxes` arrived in, so an important
 * label never loses a tie to an unrelated one that merely sorted earlier.
 * Pure and DOM-free: `studio/checks/brain.check.mjs` calls it directly with
 * a synthetic dense cluster and asserts the returned ids' boxes never touch.
 */
export function declutterLabels(boxes: LabelBox[], alwaysShow: Set<string> = new Set()): Set<string> {
  const ordered = alwaysShow.size ? [...boxes.filter((b) => alwaysShow.has(b.id)), ...boxes.filter((b) => !alwaysShow.has(b.id))] : boxes;
  const kept: LabelBox[] = [];
  const result = new Set<string>();
  for (const box of ordered) {
    if (kept.some((k) => boxesOverlap(k, box))) continue;
    kept.push(box);
    result.add(box.id);
  }
  return result;
}
