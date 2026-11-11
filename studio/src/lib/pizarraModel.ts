/**
 * Pizarra's pure model: JSON Canvas items with the `fx` extension (src/pizarra.py), patches between windows,
 * the diff that turns an edit into a patch, align / snap / stroke helpers. No React, no DOM: the screen
 * (screens/studio/Pizarra.tsx) calls these and `studio/checks/surfaces.check.mjs` tests them.
 */

export type Kind = 'sticky' | 'text' | 'rect' | 'ellipse' | 'diamond' | 'frame' | 'link' | 'image' | 'stroke';
export interface JcNode {
  id: string; type: 'text' | 'group' | 'link' | 'file';
  x: number; y: number; width: number; height: number;
  text?: string; label?: string; url?: string; file?: string; color?: string;
  fx?: { k: Kind; pts?: number[][]; sw?: number };
}
export interface JcEdge {
  id: string; fromNode: string; toNode: string; fromSide?: string; toSide?: string;
  fromEnd?: 'none' | 'arrow'; toEnd?: 'none' | 'arrow'; label?: string; color?: string;
}
export interface Doc { nodes: JcNode[]; edges: JcEdge[] }
export interface Patch { nodes?: Record<string, JcNode | null>; edges?: Record<string, JcEdge | null>; order?: string[] }
export interface Rect { x: number; y: number; width: number; height: number }

export const DEFAULT_SIZE: Record<Kind, [number, number]> = {
  sticky: [200, 140], text: [240, 48], rect: [220, 100], ellipse: [200, 110], diamond: [200, 130],
  frame: [480, 320], link: [260, 80], image: [320, 240], stroke: [60, 60],
};
export const PRESET_HEX: Record<string, string> = { '1': '#e5484d', '2': '#f76b15', '3': '#e2b714', '4': '#30a46c', '5': '#0090ff', '6': '#8e4ec6' };
export const PALETTE: { id: string; label: string }[] = [
  { id: '', label: 'Default' }, { id: '3', label: 'Yellow' }, { id: '2', label: 'Orange' }, { id: '1', label: 'Red' },
  { id: '6', label: 'Purple' }, { id: '5', label: 'Blue' }, { id: '4', label: 'Green' }, { id: '#8a8f98', label: 'Gray' },
];

export function kindOf(n: JcNode): Kind {
  const k = n.fx?.k;
  if (k) return k;
  if (n.type === 'group') return 'frame';
  if (n.type === 'link') return 'link';
  if (n.type === 'file') return 'image';
  return 'rect';
}
export const colorHex = (c?: string): string | undefined => (c ? (PRESET_HEX[c] ?? c) : undefined);
export const textOf = (n: JcNode): string => (n.type === 'group' ? n.label ?? '' : n.type === 'link' ? n.url ?? '' : n.type === 'file' ? n.file ?? '' : n.text ?? '');

const HEX = '0123456789abcdef';
export function newId(): string {
  let out = '';
  const bytes = typeof crypto !== 'undefined' && crypto.getRandomValues ? crypto.getRandomValues(new Uint8Array(8)) : Uint8Array.from({ length: 8 }, () => Math.floor(Math.random() * 256));
  for (const b of bytes) out += HEX[b >> 4] + HEX[b & 15];
  return out;
}

export function fitHeight(kind: Kind, text: string, width: number, baseH: number): number {
  if (kind === 'stroke' || kind === 'frame' || kind === 'image') return baseH;
  const cpl = Math.max(6, Math.floor((width - 28) / 8.2));
  let lines = 0;
  for (const line of text.split('\n')) lines += Math.max(1, Math.ceil(line.length / cpl));
  let need = lines * 22 + 30;
  if (kind === 'diamond') need = Math.floor(need * 1.5);
  else if (kind === 'ellipse') need = Math.floor(need * 1.3);
  return Math.max(baseH, need);
}

export function makeNode(kind: Kind, x: number, y: number, opts: { w?: number; h?: number; text?: string; color?: string; file?: string; pts?: number[][]; sw?: number } = {}): JcNode {
  const [bw, bh] = DEFAULT_SIZE[kind];
  const width = Math.max(24, Math.round(opts.w ?? bw));
  const text = opts.text ?? '';
  const height = Math.max(16, Math.round(opts.h ?? fitHeight(kind, text, width, bh)));
  const base = { id: newId(), x: Math.round(x), y: Math.round(y), width, height };
  let node: JcNode;
  if (kind === 'frame') node = { ...base, type: 'group', label: text };
  else if (kind === 'image') node = { ...base, type: 'file', file: opts.file ?? '', fx: { k: 'image' } };
  else if (kind === 'link') node = { ...base, type: 'link', url: text };
  else {
    node = { ...base, type: 'text', text };
    if (kind !== 'rect') node.fx = { k: kind };
    if (kind === 'stroke') node.fx = { k: 'stroke', pts: opts.pts ?? [], sw: opts.sw ?? 3 };
  }
  if (opts.color) node.color = opts.color;
  return node;
}

export function withText(n: JcNode, text: string): JcNode {
  if (n.type === 'group') return { ...n, label: text };
  if (n.type === 'link') return { ...n, url: text };
  if (n.type === 'file') return n;
  const next = { ...n, text };
  const k = kindOf(n);
  const h = fitHeight(k, text, n.width, DEFAULT_SIZE[k][1]);
  if (h > n.height) next.height = h;
  return next;
}

/* ── patches ── */

export const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);

/** The patches that turn `prev` into `cur` (`after`) and back (`before`); null when nothing changed. */
export function diffDocs(prev: Doc, cur: Doc): { before: Patch; after: Patch } | null {
  const before: Patch = { nodes: {}, edges: {} };
  const after: Patch = { nodes: {}, edges: {} };
  const pn = new Map(prev.nodes.map((n) => [n.id, n]));
  const cn = new Map(cur.nodes.map((n) => [n.id, n]));
  for (const [id, n] of cn) {
    const o = pn.get(id);
    if (!o || !same(o, n)) { before.nodes![id] = o ?? null; after.nodes![id] = n; }
  }
  for (const [id, o] of pn) if (!cn.has(id)) { before.nodes![id] = o; after.nodes![id] = null; }
  const pe = new Map(prev.edges.map((e) => [e.id, e]));
  const ce = new Map(cur.edges.map((e) => [e.id, e]));
  for (const [id, e] of ce) {
    const o = pe.get(id);
    if (!o || !same(o, e)) { before.edges![id] = o ?? null; after.edges![id] = e; }
  }
  for (const [id, o] of pe) if (!ce.has(id)) { before.edges![id] = o; after.edges![id] = null; }
  const po = prev.nodes.map((n) => n.id).filter((id) => cn.has(id));
  const co = cur.nodes.map((n) => n.id).filter((id) => pn.has(id));
  // The stacking order travels only when it changed, or when an item came back / appeared somewhere other than the top.
  const prevIds = prev.nodes.map((n) => n.id), curIds = cur.nodes.map((n) => n.id);
  const added = curIds.filter((id) => !pn.has(id)), removed = prevIds.filter((id) => !cn.has(id));
  const addedOnTop = added.every((id, i) => curIds[curIds.length - added.length + i] === id);
  const removedOnTop = removed.every((id, i) => prevIds[prevIds.length - removed.length + i] === id);
  if (!same(po, co) || !addedOnTop || !removedOnTop) { before.order = prevIds; after.order = curIds; }
  const empty = !Object.keys(after.nodes!).length && !Object.keys(after.edges!).length && !after.order;
  return empty ? null : { before, after };
}

/** `patch` applied to `doc` (a new doc). Items listed in `keep` are left as they are (the person is moving them). */
export function applyPatch(doc: Doc, patch: Patch, keep?: Set<string>): Doc {
  const nodes = new Map(doc.nodes.map((n) => [n.id, n]));
  const order = doc.nodes.map((n) => n.id);
  for (const [id, n] of Object.entries(patch.nodes ?? {})) {
    if (keep?.has(id) && n) continue;
    if (n === null) nodes.delete(id);
    else { if (!nodes.has(id)) order.push(id); nodes.set(id, n); }
  }
  const edges = new Map(doc.edges.map((e) => [e.id, e]));
  for (const [id, e] of Object.entries(patch.edges ?? {})) {
    if (e === null) edges.delete(id);
    else edges.set(id, e);
  }
  let ids = order.filter((id) => nodes.has(id));
  if (patch.order) {
    const pos = new Map(patch.order.map((id, i) => [id, i]));
    ids = [...ids].sort((a, b) => (pos.get(a) ?? 1e9) - (pos.get(b) ?? 1e9));
  }
  return { nodes: ids.map((id) => nodes.get(id)!), edges: [...edges.values()].filter((e) => nodes.has(e.fromNode) && nodes.has(e.toNode)) };
}

/** `later` laid over `earlier` (per item the last write wins). */
export function mergePatches(earlier: Patch, later: Patch): Patch {
  return { nodes: { ...earlier.nodes, ...later.nodes }, edges: { ...earlier.edges, ...later.edges }, ...(later.order ? { order: later.order } : earlier.order ? { order: earlier.order } : {}) };
}
export const patchSize = (p: Patch) => Object.keys(p.nodes ?? {}).length + Object.keys(p.edges ?? {}).length + (p.order ? 1 : 0);

/* ── geometry ── */

export const bounds = (rects: Rect[]): Rect | null => {
  if (!rects.length) return null;
  const x0 = Math.min(...rects.map((r) => r.x)), y0 = Math.min(...rects.map((r) => r.y));
  const x1 = Math.max(...rects.map((r) => r.x + r.width)), y1 = Math.max(...rects.map((r) => r.y + r.height));
  return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
};

export type AlignHow = 'left' | 'center' | 'right' | 'top' | 'middle' | 'bottom' | 'distribute-h' | 'distribute-v';
/** New top-left positions for `how`; only items that move are returned. */
export function alignRects(items: (Rect & { id: string })[], how: AlignHow): Record<string, { x: number; y: number }> {
  const b = bounds(items);
  const out: Record<string, { x: number; y: number }> = {};
  if (!b || items.length < 2) return out;
  const put = (it: Rect & { id: string }, x: number, y: number) => { if (x !== it.x || y !== it.y) out[it.id] = { x: Math.round(x), y: Math.round(y) }; };
  if (how === 'left') items.forEach((it) => put(it, b.x, it.y));
  else if (how === 'right') items.forEach((it) => put(it, b.x + b.width - it.width, it.y));
  else if (how === 'center') items.forEach((it) => put(it, b.x + (b.width - it.width) / 2, it.y));
  else if (how === 'top') items.forEach((it) => put(it, it.x, b.y));
  else if (how === 'bottom') items.forEach((it) => put(it, it.x, b.y + b.height - it.height));
  else if (how === 'middle') items.forEach((it) => put(it, it.x, b.y + (b.height - it.height) / 2));
  else if (how === 'distribute-h' && items.length > 2) {
    const s = [...items].sort((a, c) => a.x - c.x);
    const gap = (b.width - s.reduce((n, it) => n + it.width, 0)) / (s.length - 1);
    let cur = b.x;
    for (const it of s) { put(it, cur, it.y); cur += it.width + gap; }
  } else if (how === 'distribute-v' && items.length > 2) {
    const s = [...items].sort((a, c) => a.y - c.y);
    const gap = (b.height - s.reduce((n, it) => n + it.height, 0)) / (s.length - 1);
    let cur = b.y;
    for (const it of s) { put(it, it.x, cur); cur += it.height + gap; }
  }
  return out;
}

export interface Guide { axis: 'x' | 'y'; at: number; from: number; to: number }
/** Snap a moving box to the edges / centres of `others` within `threshold`; returns the shift and the guides to draw. */
export function snapBox(moving: Rect, others: Rect[], threshold: number): { dx: number; dy: number; guides: Guide[] } {
  const mx = [moving.x, moving.x + moving.width / 2, moving.x + moving.width];
  const my = [moving.y, moving.y + moving.height / 2, moving.y + moving.height];
  let bx: { d: number; at: number; o: Rect } | null = null;
  let by: { d: number; at: number; o: Rect } | null = null;
  for (const o of others) {
    const ox = [o.x, o.x + o.width / 2, o.x + o.width];
    const oy = [o.y, o.y + o.height / 2, o.y + o.height];
    for (const a of mx) for (const c of ox) { const d = c - a; if (Math.abs(d) <= threshold && (!bx || Math.abs(d) < Math.abs(bx.d))) bx = { d, at: c, o }; }
    for (const a of my) for (const c of oy) { const d = c - a; if (Math.abs(d) <= threshold && (!by || Math.abs(d) < Math.abs(by.d))) by = { d, at: c, o }; }
  }
  const dx = bx?.d ?? 0, dy = by?.d ?? 0;
  const m = { x: moving.x + dx, y: moving.y + dy, width: moving.width, height: moving.height };
  const guides: Guide[] = [];
  if (bx) guides.push({ axis: 'x', at: bx.at, from: Math.min(m.y, bx.o.y), to: Math.max(m.y + m.height, bx.o.y + bx.o.height) });
  if (by) guides.push({ axis: 'y', at: by.at, from: Math.min(m.x, by.o.x), to: Math.max(m.x + m.width, by.o.x + by.o.width) });
  return { dx, dy, guides };
}

/** Where a line from the centre of `r` towards (tx, ty) leaves its border (connectors stay attached on any side). */
export function borderPoint(r: Rect, tx: number, ty: number, shape: Kind = 'rect'): { x: number; y: number } {
  const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
  const dx = tx - cx, dy = ty - cy;
  if (!dx && !dy) return { x: cx, y: cy };
  const hw = r.width / 2, hh = r.height / 2;
  let s: number;
  if (shape === 'ellipse') s = 1 / Math.sqrt((dx * dx) / (hw * hw) + (dy * dy) / (hh * hh));
  else if (shape === 'diamond') s = 1 / (Math.abs(dx) / hw + Math.abs(dy) / hh);
  else s = Math.min(dx ? hw / Math.abs(dx) : Infinity, dy ? hh / Math.abs(dy) : Infinity);
  return { x: cx + dx * s, y: cy + dy * s };
}

/** Ramer-Douglas-Peucker: a freehand stroke with fewer points, same look. */
export function simplifyStroke(pts: number[][], tol = 1.2): number[][] {
  if (pts.length < 3) return pts;
  const keep = new Array<boolean>(pts.length).fill(false);
  keep[0] = keep[pts.length - 1] = true;
  const stack: [number, number][] = [[0, pts.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop()!;
    let worst = -1, wi = -1;
    const [ax, ay] = pts[a], [bx, by] = pts[b];
    const len = Math.hypot(bx - ax, by - ay) || 1;
    for (let i = a + 1; i < b; i++) {
      const d = Math.abs((bx - ax) * (ay - pts[i][1]) - (ax - pts[i][0]) * (by - ay)) / len;
      if (d > worst) { worst = d; wi = i; }
    }
    if (worst > tol && wi > 0) { keep[wi] = true; stack.push([a, wi], [wi, b]); }
  }
  return pts.filter((_, i) => keep[i]);
}

/** A stroke node from points in flow coordinates: positioned at their bounding box, points relative to it. */
export function strokeNode(points: number[][], color: string | undefined, sw = 3): JcNode | null {
  const pts = simplifyStroke(points);
  if (pts.length < 2) return null;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x = Math.min(...xs), y = Math.min(...ys);
  const w = Math.max(8, Math.max(...xs) - x), h = Math.max(8, Math.max(...ys) - y);
  return makeNode('stroke', x, y, { w, h, color, sw, pts: pts.map((p) => [Math.round(p[0] - x), Math.round(p[1] - y)]) });
}
export const strokePath = (pts: number[][]): string => (pts.length ? 'M' + pts.map((p) => `${p[0]} ${p[1]}`).join(' L') : '');

/** Copies of `ids` (and the connectors between them) shifted by (dx, dy), with new ids. */
export function duplicateItems(doc: Doc, ids: Set<string>, dx = 32, dy = 32): { nodes: JcNode[]; edges: JcEdge[]; ids: string[] } {
  const map = new Map<string, string>();
  const nodes = doc.nodes.filter((n) => ids.has(n.id)).map((n) => {
    const id = newId();
    map.set(n.id, id);
    return { ...n, id, x: n.x + dx, y: n.y + dy };
  });
  const edges = doc.edges.filter((e) => map.has(e.fromNode) && map.has(e.toNode)).map((e) => ({ ...e, id: newId(), fromNode: map.get(e.fromNode)!, toNode: map.get(e.toNode)! }));
  return { nodes, edges, ids: [...map.values()] };
}

/** A frame around `ids` (title header included), put behind them. */
export function frameAround(nodes: JcNode[], title = ''): JcNode | null {
  const b = bounds(nodes);
  if (!b) return null;
  const pad = 40, head = 32;
  return makeNode('frame', b.x - pad, b.y - pad - head, { w: b.width + pad * 2, h: b.height + pad * 2 + head, text: title });
}

export const framesHolding = (doc: Doc, frame: JcNode): string[] =>
  doc.nodes.filter((n) => n.id !== frame.id && n.x + n.width / 2 >= frame.x && n.x + n.width / 2 <= frame.x + frame.width && n.y + n.height / 2 >= frame.y && n.y + n.height / 2 <= frame.y + frame.height).map((n) => n.id);
