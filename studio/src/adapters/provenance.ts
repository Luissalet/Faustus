/**
 * Provenance (`/api/provenance`): the audit graph whose every edge was read
 * from a stored record — a declared dependency, an evidence span, a
 * checkpoint diff, a citation, a verified text overlap. Nothing a model
 * asserted. The pure logic lives in `lib/graph.ts`.
 */
import { ApiError, getJson } from './api';
import { edgeFrom, encodeNodeId, graphFrom, intOf, linesFrom, nodeFrom, type Graph, type GraphNode } from '../lib/graph';

export interface SourceRow {
  name: string;
  available: boolean;
  count: number | null;
  note: string;
}

export interface GraphPayload extends Graph {
  enabled: boolean;
  truncated: boolean;
  limit: number | null;
  sources: SourceRow[];
  nodeKinds: string[];
  edgeKinds: string[];
  stats: { nodes: number; edges: number; orphans: number };
}

export interface Scope {
  project?: string;
  workspace?: string;
  limit?: number;
}

export interface Step {
  order: number;
  hop: number;
  from: string;
  to: string;
  kind: string;
  confidence: number | null;
  trust: string;
  why: string;
  direction: 'rests_on' | 'vouches_for';
  node: GraphNode | null;
  meta: Record<string, unknown>;
}

export interface Explain {
  node: GraphNode | null;
  steps: Step[];
  summary: string;
}

export interface Neighbors extends Graph {
  root: string;
  hops: number;
  impact: GraphNode[];
}

export interface Duplicate {
  a: string;
  b: string;
  aLabel: string;
  bLabel: string;
  ratio: number | null;
  why: string;
  spans: [[number, number], [number, number]][];
}

export interface Orphans {
  byKind: Record<string, GraphNode[]>;
  count: number;
  duplicates: Duplicate[];
  enabled: boolean;
}

const isObj = (v: unknown): v is Record<string, unknown> => Boolean(v) && typeof v === 'object' && !Array.isArray(v);

function scopeQuery(scope: Scope, extra: Record<string, string> = {}): string {
  const p = new URLSearchParams();
  if (scope.project?.trim()) p.set('project', scope.project.trim());
  if (scope.workspace?.trim()) p.set('workspace', scope.workspace.trim());
  if (scope.limit) p.set('limit', String(scope.limit));
  for (const [k, v] of Object.entries(extra)) if (v) p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : '';
}

function sourcesFrom(raw: unknown): SourceRow[] {
  const rows = isObj(raw) ? raw : {};
  return Object.keys(rows)
    .sort()
    .map((name) => {
      const row = isObj(rows[name]) ? (rows[name] as Record<string, unknown>) : {};
      return { name, available: Boolean(row.available), count: intOf(row.count), note: String(row.note ?? '') };
    });
}

async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  try {
    return await getJson<T>(path, signal);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) throw new ApiError('No such node in the provenance graph.', 404);
    throw e;
  }
}

export async function loadGraph(scope: Scope, signal?: AbortSignal): Promise<GraphPayload> {
  const raw = await read<Record<string, unknown>>(`/api/provenance/graph${scopeQuery(scope)}`, signal);
  const g = graphFrom(raw);
  const stats = isObj(raw.stats) ? raw.stats : {};
  const connected = new Set<string>();
  for (const e of g.edges) {
    connected.add(e.from);
    connected.add(e.to);
  }
  return {
    ...g,
    enabled: raw.enabled !== false,
    truncated: Boolean(raw.truncated),
    limit: intOf(raw.limit),
    sources: sourcesFrom(raw.sources),
    nodeKinds: Array.isArray(raw.node_kinds) ? raw.node_kinds.map(String) : [],
    edgeKinds: Array.isArray(raw.edge_kinds) ? raw.edge_kinds.map(String) : [],
    stats: { nodes: intOf(stats.nodes) ?? g.nodes.length, edges: intOf(stats.edges) ?? g.edges.length, orphans: intOf(stats.orphans) ?? g.nodes.filter((n) => !connected.has(n.id)).length },
  };
}

function stepFrom(raw: unknown, index: number): Step {
  const row = isObj(raw) ? raw : {};
  const e = edgeFrom(row);
  return {
    order: intOf(row.order) ?? index + 1,
    hop: intOf(row.hop) ?? 1,
    from: e.from,
    to: e.to,
    kind: e.kind,
    confidence: e.confidence,
    trust: e.trust,
    why: e.why,
    direction: row.direction === 'vouches_for' ? 'vouches_for' : 'rests_on',
    node: isObj(row.node) ? nodeFrom(row.node) : null,
    meta: e.meta,
  };
}

export async function explainNode(id: string, scope: Scope, signal?: AbortSignal): Promise<Explain> {
  const raw = await read<Record<string, unknown>>(`/api/provenance/node/${encodeNodeId(id)}/explain${scopeQuery(scope)}`, signal);
  const node = isObj(raw.node) ? nodeFrom(raw.node) : null;
  return { node: node && node.id ? node : null, steps: (Array.isArray(raw.steps) ? raw.steps : []).map(stepFrom), summary: String(raw.summary ?? '') };
}

export async function nodeNeighbors(id: string, hops: number, scope: Scope, signal?: AbortSignal): Promise<Neighbors> {
  const raw = await read<Record<string, unknown>>(`/api/provenance/node/${encodeNodeId(id)}/neighbors${scopeQuery(scope, { hops: String(hops) })}`, signal);
  const g = graphFrom(raw);
  const impact = (Array.isArray(raw.impact) ? raw.impact : []).map(nodeFrom).filter((n) => n.id);
  const known = new Set(impact.map((n) => n.id));
  for (const id2 of Array.isArray(raw.impact_ids) ? raw.impact_ids.map(String) : []) {
    if (id2 && !known.has(id2)) {
      known.add(id2);
      impact.push(nodeFrom({ id: id2, kind: 'node', label: id2 }));
    }
  }
  return { ...g, root: String(raw.root ?? id), hops: intOf(raw.hops) ?? hops, impact };
}

export async function loadOrphans(scope: Scope, signal?: AbortSignal): Promise<Orphans> {
  const raw = await read<Record<string, unknown>>(`/api/provenance/orphans${scopeQuery(scope)}`, signal);
  const grouped = isObj(raw.orphans) ? raw.orphans : {};
  const byKind: Record<string, GraphNode[]> = {};
  for (const kind of Object.keys(grouped).sort()) {
    const rows = (Array.isArray(grouped[kind]) ? (grouped[kind] as unknown[]) : []).map(nodeFrom).filter((n) => n.id);
    if (rows.length) byKind[kind] = rows;
  }
  const duplicates: Duplicate[] = (Array.isArray(raw.duplicates) ? raw.duplicates : [])
    .map((d): Duplicate => {
      const row = isObj(d) ? d : {};
      const spans: Duplicate['spans'] = [];
      for (const span of Array.isArray(row.spans) ? row.spans : []) {
        if (!Array.isArray(span) || span.length < 2) continue;
        const a = Array.isArray(span[0]) ? span[0] : [];
        const b = Array.isArray(span[1]) ? span[1] : [];
        const vals = [a[0], a[1], b[0], b[1]].map(intOf);
        if (vals.some((v) => v === null)) continue;
        spans.push([[vals[0]!, vals[1]!], [vals[2]!, vals[3]!]]);
      }
      const a = String(row.a ?? '');
      const b = String(row.b ?? '');
      const ratio = row.ratio === null || row.ratio === undefined ? null : Number.isFinite(Number(row.ratio)) ? Number(row.ratio) : null;
      return { a, b, aLabel: String(row.a_label || a), bLabel: String(row.b_label || b), ratio, why: String(row.why ?? ''), spans };
    })
    .filter((d) => d.a && d.b);
  const listed = Object.values(byKind).reduce((n, rows) => n + rows.length, 0);
  return { byKind, count: intOf(raw.count) ?? listed, duplicates, enabled: raw.enabled !== false };
}

/* ── explain helpers (ported) ── */

function linesFromWhy(step: Step): number[] {
  const node = step.node;
  if (!node || node.kind !== 'file') return [];
  const path = String(node.meta.path || node.detail || '');
  if (!path) return [];
  const needle = `${path}:`;
  const out: number[] = [];
  let at = step.why.indexOf(needle);
  while (at !== -1) {
    const digits = /^(\d+)/.exec(step.why.slice(at + needle.length));
    if (digits) {
      const n = intOf(digits[1]);
      if (n !== null && !out.includes(n)) out.push(n);
    }
    at = step.why.indexOf(needle, at + 1);
  }
  return out.sort((a, b) => a - b);
}

function stepLineInfo(step: Step): { lines: number[]; source: 'edge' | 'why' | 'node' } {
  const fromEdge = linesFrom(step.meta);
  if (fromEdge.length) return { lines: fromEdge, source: 'edge' };
  const fromWhy = linesFromWhy(step);
  if (fromWhy.length) return { lines: fromWhy, source: 'why' };
  return { lines: linesFrom(step.node?.meta), source: 'node' };
}

/** "src/app.py line 120", "chat session a1b2…", "job 7" — where the step lands. */
export function stepWhere(step: Step): string {
  const node = step.node;
  if (!node) return '';
  const meta = node.meta;
  const info = stepLineInfo(step);
  if (node.kind === 'file') {
    const path = String(meta.path || node.detail || node.label || '');
    if (!info.lines.length) return path;
    if (info.source === 'node' && info.lines.length > 1) return `${path} — lines recorded here: ${info.lines.join(', ')}`;
    return `${path} line${info.lines.length > 1 ? 's' : ''} ${info.lines.join(', ')}`;
  }
  if (node.kind === 'chat') {
    const session = String(meta.session_id || '');
    return session && !node.label.includes(session) ? `chat session ${session}` : '';
  }
  if (node.kind === 'checkpoint') {
    const job = String(meta.job_id || '');
    return job && !node.label.includes(job) ? `dispatch job ${job}` : '';
  }
  if (node.kind === 'corpus') {
    const slug = String(meta.slug || '');
    if (!slug) return '';
    const source = String(meta.source || '');
    return source && source !== node.label ? `${source} · ${slug} corpus` : `${slug} corpus`;
  }
  return '';
}

const TERMINUS_RANK: Record<string, number> = { file: 0, chat: 1, corpus: 2, checkpoint: 3 };

/** The nearest step that lands on something a human can open. */
export function terminus(steps: Step[]): { step: Step; node: GraphNode; text: string } | null {
  let best: Step | null = null;
  let bestScore = Infinity;
  for (const step of steps) {
    const kind = step.node?.kind ?? '';
    if (!(kind in TERMINUS_RANK)) continue;
    const score = step.hop * 100 + TERMINUS_RANK[kind] * 10 + Math.min(9, step.order);
    if (score < bestScore) {
      bestScore = score;
      best = step;
    }
  }
  if (!best || !best.node) return null;
  return { step: best, node: best.node, text: stepWhere(best) };
}

export const META_KEYS = ['status', 'priority', 'level', 'maturity', 'trust_class', 'effective_score', 'harmful_ratio', 'path', 'session_id', 'job_id', 'slug', 'source', 'project', 'owner', 'updated_at', 'workspace', 'verdict'];

export function confidenceLabel(v: number | null): string {
  return v === null ? '—' : v.toFixed(2);
}

export function percentLabel(v: number | null): string {
  return v === null ? '—' : `${Math.round(v * 100)}%`;
}

export function duplicateExcerpt(why: string): string {
  const m = /[“"]([^“”"]+)[”"]\s*$/.exec(why.trim());
  return m ? m[1] : '';
}

export function duplicateSpansText(spans: Duplicate['spans']): string {
  if (!spans.length) return '';
  const shown = spans
    .slice(0, 4)
    .map(([[a0, a1], [b0, b1]]) => `${a0}–${a1} ↔ ${b0}–${b1}`)
    .join(', ');
  const rest = spans.length > 4 ? `, +${spans.length - 4} more` : '';
  return `${spans.length} verified span(s), in normalized characters: ${shown}${rest}`;
}
