/**
 * The provenance graph's pure logic: normalising the server's rows, the
 * view model (kind filter, search, the drawing cap, which labels stay), and
 * the deterministic force layout. Ported from the previous interface's
 * provenance.js; no DOM, no clock, no randomness — the same graph always
 * lays out the same way.
 */

export interface GraphNode {
  id: string;
  kind: string;
  label: string;
  detail: string;
  meta: Record<string, unknown>;
}

export interface GraphEdge {
  from: string;
  to: string;
  kind: string;
  confidence: number | null;
  trust: string;
  why: string;
  meta: Record<string, unknown>;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export const NODE_KINDS = ['objective', 'memory', 'chat', 'file', 'checkpoint', 'expert', 'corpus'] as const;
export const EDGE_KINDS = ['depends_on', 'evidence_of', 'contradicts', 'changed', 'cites', 'contains', 'duplicate_of'] as const;

export const KIND_NOTE: Record<string, string> = {
  objective: 'a goal declared in objectives.jsonl',
  memory: 'a standing rule or fact the agent remembers',
  chat: 'a chat session',
  file: 'a file in the bound workspace',
  checkpoint: 'a dispatched job and the diff it left on disk',
  expert: 'a specialist with its own corpus',
  corpus: 'a file inside an expert corpus',
};

export const MAX_DRAWN = 200;
const LABEL_ALL = 60;
const LABEL_TOP = 24;
export const LAYOUT = { width: 1000, height: 680, iterations: 140, pad: 46 };

export const token = (v: unknown): string =>
  String(v ?? '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-');
const isObj = (v: unknown): v is Record<string, unknown> => Boolean(v) && typeof v === 'object' && !Array.isArray(v);
const num = (v: unknown): number | null => (v === null || v === undefined || v === '' ? null : Number.isFinite(Number(v)) ? Number(v) : null);
export const intOf = (v: unknown): number | null => {
  const n = num(v);
  return n === null ? null : Math.trunc(n);
};

export function nodeFrom(raw: unknown): GraphNode {
  const row = isObj(raw) ? raw : {};
  const id = String(row.id ?? '');
  return { id, kind: token(row.kind) || 'node', label: row.label === undefined || row.label === null || row.label === '' ? id : String(row.label), detail: String(row.detail ?? ''), meta: isObj(row.meta) ? row.meta : {} };
}

export function edgeFrom(raw: unknown): GraphEdge {
  const row = isObj(raw) ? raw : {};
  return { from: String(row.from ?? ''), to: String(row.to ?? ''), kind: token(row.kind) || 'edge', confidence: num(row.confidence), trust: String(row.trust || 'declared'), why: String(row.why ?? ''), meta: isObj(row.meta) ? row.meta : {} };
}

export function graphFrom(raw: unknown): Graph {
  const src = isObj(raw) ? raw : {};
  const nodes = (Array.isArray(src.nodes) ? src.nodes : []).map(nodeFrom).filter((n) => n.id);
  const known = new Set(nodes.map((n) => n.id));
  const edges = (Array.isArray(src.edges) ? src.edges : []).map(edgeFrom).filter((e) => known.has(e.from) && known.has(e.to));
  return { nodes, edges };
}

export function countByKind(nodes: GraphNode[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const n of nodes) out[n.kind] = (out[n.kind] || 0) + 1;
  return out;
}

export function degreeMap(edges: GraphEdge[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const e of edges) {
    out[e.from] = (out[e.from] || 0) + 1;
    out[e.to] = (out[e.to] || 0) + 1;
  }
  return out;
}

export function haystack(node: GraphNode): string {
  const m = node.meta;
  return [node.label, node.detail, node.id, node.kind, m.path, m.title, m.status, m.session_id, m.job_id, m.slug, m.source]
    .filter((p) => p !== null && p !== undefined && p !== '')
    .join(' ')
    .toLowerCase();
}

/** Every whitespace-separated term must appear; an empty query matches nothing. */
export function matches(node: GraphNode, query: string): boolean {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return false;
  const hay = haystack(node);
  return terms.every((term) => hay.includes(term));
}

export function filterKinds(g: Graph, kinds: string[]): Graph {
  const wanted = new Set(kinds.map(token).filter(Boolean));
  if (!wanted.size) return { nodes: g.nodes.slice(), edges: g.edges.slice() };
  const nodes = g.nodes.filter((n) => wanted.has(n.kind));
  const keep = new Set(nodes.map((n) => n.id));
  return { nodes, edges: g.edges.filter((e) => keep.has(e.from) && keep.has(e.to)) };
}

/**
 * Cut the drawing down to something legible: the best connected nodes stay,
 * ties broken on (kind, id) so two renders draw the same picture; anything
 * pinned (the selection, the search hits) stays whatever its degree.
 */
export function capGraph(nodes: GraphNode[], edges: GraphEdge[], limit = MAX_DRAWN, pinned: string[] = []): Graph & { shown: number; total: number; capped: boolean } {
  const max = Math.max(1, limit);
  if (nodes.length <= max) return { nodes: nodes.slice(), edges: edges.slice(), shown: nodes.length, total: nodes.length, capped: false };
  const degrees = degreeMap(edges);
  const keepFirst = new Set(pinned.filter(Boolean));
  const ranked = nodes.slice().sort((a, b) => {
    const pinA = keepFirst.has(a.id) ? 1 : 0;
    const pinB = keepFirst.has(b.id) ? 1 : 0;
    if (pinA !== pinB) return pinB - pinA;
    const degA = degrees[a.id] || 0;
    const degB = degrees[b.id] || 0;
    if (degA !== degB) return degB - degA;
    if (a.kind !== b.kind) return a.kind < b.kind ? -1 : 1;
    return a.id < b.id ? -1 : 1;
  });
  const kept = ranked.slice(0, max).sort((a, b) => (a.kind !== b.kind ? (a.kind < b.kind ? -1 : 1) : a.id < b.id ? -1 : 1));
  const keep = new Set(kept.map((n) => n.id));
  return { nodes: kept, edges: edges.filter((e) => keep.has(e.from) && keep.has(e.to)), shown: kept.length, total: nodes.length, capped: true };
}

export function pickLabelIds(nodes: GraphNode[], degrees: Record<string, number>, opts: { selected?: string; matched?: string[] } = {}): string[] {
  if (nodes.length <= LABEL_ALL) return nodes.map((n) => n.id);
  const keep = new Set<string>();
  if (opts.selected) keep.add(opts.selected);
  for (const id of opts.matched ?? []) keep.add(id);
  const busiest = nodes
    .slice()
    .sort((a, b) => {
      const degA = degrees[a.id] || 0;
      const degB = degrees[b.id] || 0;
      if (degA !== degB) return degB - degA;
      return a.id < b.id ? -1 : 1;
    })
    .slice(0, LABEL_TOP);
  for (const n of busiest) keep.add(n.id);
  return nodes.filter((n) => keep.has(n.id)).map((n) => n.id);
}

export interface ViewModel {
  nodes: GraphNode[];
  edges: GraphEdge[];
  degrees: Record<string, number>;
  matched: string[];
  labels: Set<string>;
  shown: number;
  total: number;
  filteredTotal: number;
  capped: boolean;
  query: string;
  selected: string;
}

/** Everything the canvas needs, decided once. */
export function viewModel(g: Graph, state: { kinds?: string[]; query?: string; selected?: string; drawLimit?: number }): ViewModel {
  const filtered = filterKinds(g, state.kinds ?? []);
  const query = (state.query ?? '').trim();
  const hits = query ? filtered.nodes.filter((n) => matches(n, query)).map((n) => n.id) : [];
  const pinned = hits.slice();
  if (state.selected) pinned.push(state.selected);
  const capped = capGraph(filtered.nodes, filtered.edges, state.drawLimit ?? MAX_DRAWN, pinned);
  const degrees = degreeMap(capped.edges);
  const drawn = new Set(capped.nodes.map((n) => n.id));
  const matched = hits.filter((id) => drawn.has(id));
  return {
    nodes: capped.nodes,
    edges: capped.edges,
    degrees,
    matched,
    labels: new Set(pickLabelIds(capped.nodes, degrees, { matched, selected: state.selected })),
    shown: capped.shown,
    total: g.nodes.length,
    filteredTotal: capped.total,
    capped: capped.capped,
    query,
    selected: state.selected ?? '',
  };
}

/* ── the deterministic force layout ── */

/** FNV-1a as a fraction in [0, 1): the layout's only source of "randomness". */
export function hash01(value: string): number {
  let h = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) / 4294967296;
}

export type Point = { x: number; y: number };
export type Positions = Record<string, Point>;

function seed(nodes: GraphNode[], opts: typeof LAYOUT): Positions {
  const kinds: string[] = [];
  for (const n of nodes) if (!kinds.includes(n.kind)) kinds.push(n.kind);
  kinds.sort();
  const cx = opts.width / 2;
  const cy = opts.height / 2;
  const radius = Math.min(opts.width, opts.height) * 0.42;
  const out: Positions = {};
  for (const n of nodes) {
    const band = Math.max(1, kinds.length);
    const angle = 2 * Math.PI * ((kinds.indexOf(n.kind) + 0.15 + 0.7 * hash01(n.id)) / band);
    const spread = 0.32 + 0.68 * hash01(`r:${n.id}`);
    out[n.id] = { x: cx + Math.cos(angle) * radius * spread, y: cy + Math.sin(angle) * radius * spread };
  }
  return out;
}

function fit(positions: Positions, ids: string[], opts: typeof LAYOUT): Positions {
  if (!ids.length) return positions;
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const id of ids) {
    const p = positions[id];
    minX = Math.min(minX, p.x);
    maxX = Math.max(maxX, p.x);
    minY = Math.min(minY, p.y);
    maxY = Math.max(maxY, p.y);
  }
  const spanX = Math.max(maxX - minX, 1);
  const spanY = Math.max(maxY - minY, 1);
  const usableX = Math.max(opts.width - opts.pad * 2, 1);
  const usableY = Math.max(opts.height - opts.pad * 2, 1);
  // Shrink a cloud that overflows; never blow a handful of nodes up to the
  // corners (a four-node graph should read as a small cluster, not a frame).
  const scale = Math.min(usableX / spanX, usableY / spanY, 1);
  const offX = opts.pad + (usableX - spanX * scale) / 2;
  const offY = opts.pad + (usableY - spanY * scale) / 2;
  const out: Positions = {};
  for (const id of ids) out[id] = { x: offX + (positions[id].x - minX) * scale, y: offY + (positions[id].y - minY) * scale };
  return out;
}

function round(positions: Positions): Positions {
  const out: Positions = {};
  for (const id of Object.keys(positions)) out[id] = { x: Math.round(positions[id].x * 10) / 10, y: Math.round(positions[id].y * 10) / 10 };
  return out;
}

/**
 * A small Fruchterman–Reingold pass: repulsion between every pair, springs
 * along the edges, a little gravity, a cooling schedule.
 */
export function layout(nodes: GraphNode[], edges: GraphEdge[], options: Partial<typeof LAYOUT> = {}): { width: number; height: number; positions: Positions } {
  const opts = { ...LAYOUT, ...options };
  const positions = seed(nodes, opts);
  const ids = nodes.map((n) => n.id);
  const known = new Set(ids);
  const links = edges.filter((e) => known.has(e.from) && known.has(e.to) && e.from !== e.to);
  if (ids.length <= 1) {
    for (const id of ids) positions[id] = { x: opts.width / 2, y: opts.height / 2 };
    return { width: opts.width, height: opts.height, positions: round(positions) };
  }
  const ideal = Math.sqrt((opts.width * opts.height) / ids.length) * 0.72;
  const cx = opts.width / 2;
  const cy = opts.height / 2;
  let temperature = Math.min(opts.width, opts.height) * 0.12;
  const budget = options.iterations ? Math.trunc(options.iterations) : Math.max(60, Math.min(LAYOUT.iterations, Math.round(12000 / ids.length)));
  const iterations = Math.max(1, budget);
  for (let step = 0; step < iterations; step += 1) {
    const dx: Record<string, number> = {};
    const dy: Record<string, number> = {};
    for (const id of ids) {
      dx[id] = 0;
      dy[id] = 0;
    }
    for (let i = 0; i < ids.length; i += 1) {
      for (let j = i + 1; j < ids.length; j += 1) {
        const a = positions[ids[i]];
        const b = positions[ids[j]];
        let ddx = a.x - b.x;
        let ddy = a.y - b.y;
        let dist = Math.sqrt(ddx * ddx + ddy * ddy);
        if (dist < 0.01) {
          ddx = (hash01(ids[i]) - 0.5) * 0.1 || 0.05;
          ddy = (hash01(ids[j]) - 0.5) * 0.1 || 0.05;
          dist = Math.sqrt(ddx * ddx + ddy * ddy) || 0.01;
        }
        const force = Math.min((ideal * ideal) / dist, ideal * 4);
        const fx = (ddx / dist) * force;
        const fy = (ddy / dist) * force;
        dx[ids[i]] += fx;
        dy[ids[i]] += fy;
        dx[ids[j]] -= fx;
        dy[ids[j]] -= fy;
      }
    }
    for (const e of links) {
      const a = positions[e.from];
      const b = positions[e.to];
      const ddx = a.x - b.x;
      const ddy = a.y - b.y;
      const dist = Math.sqrt(ddx * ddx + ddy * ddy) || 0.01;
      const force = (dist * dist) / ideal;
      const fx = (ddx / dist) * force;
      const fy = (ddy / dist) * force;
      dx[e.from] -= fx;
      dy[e.from] -= fy;
      dx[e.to] += fx;
      dy[e.to] += fy;
    }
    for (const id of ids) {
      const p = positions[id];
      dx[id] += (cx - p.x) * 0.012;
      dy[id] += (cy - p.y) * 0.012;
      const move = Math.sqrt(dx[id] * dx[id] + dy[id] * dy[id]) || 0.0001;
      const capped = Math.min(move, temperature);
      p.x += (dx[id] / move) * capped;
      p.y += (dy[id] / move) * capped;
    }
    temperature = Math.max(temperature * 0.94, 0.6);
  }
  return { width: opts.width, height: opts.height, positions: round(fit(positions, ids, opts)) };
}

/** Busier node, bigger dot — degree is the only size signal, and it is capped. */
export function nodeRadius(degree: number | undefined): number {
  return Math.round((8 + Math.min(8, Math.sqrt(Math.max(0, degree ?? 0)) * 2.6)) * 10) / 10;
}

export function shorten(value: string, limit = 40): string {
  return value.length <= limit ? value : `${value.slice(0, limit - 1)}…`;
}

export function tooltip(node: GraphNode): string {
  const m = node.meta;
  const parts = [`${node.kind} · ${node.label}`];
  if (node.detail && node.detail !== node.label) parts.push(node.detail);
  if (typeof m.path === 'string' && m.path !== node.label && m.path !== node.detail) parts.push(m.path);
  if (Array.isArray(m.lines) && m.lines.length) parts.push(`line${m.lines.length > 1 ? 's' : ''} ${m.lines.join(', ')}`);
  if (m.session_id) parts.push(`session ${String(m.session_id)}`);
  if (m.job_id) parts.push(`job ${String(m.job_id)}`);
  parts.push(node.id);
  return parts.join(' — ');
}

/** A node id as a URL path for `/node/{node_id:path}/…`: slashes stay separators. */
export function encodeNodeId(id: string): string {
  return id.split('/').map(encodeURIComponent).join('/');
}

export function linesFrom(meta: Record<string, unknown> | undefined): number[] {
  const out: number[] = [];
  const row = meta ?? {};
  const push = (v: unknown) => {
    const n = intOf(v);
    if (n !== null && n > 0 && !out.includes(n)) out.push(n);
  };
  push(row.line);
  for (const v of Array.isArray(row.lines) ? row.lines : []) push(v);
  return out.sort((a, b) => a - b);
}
