import { getJson } from './api';

/**
 * CMP-04 — the knowledge neighborhood: `requirement -> decision -> symbol ->
 * test -> run`, typed and honest about staleness
 * (`src/knowledge_neighborhood.py`, `routes/knowledge_routes.py`). This
 * adapter only shapes the one read; the server stays authoritative on
 * requirements, the board and the code index (DECISIONES_UI.md, "no
 * duplicar APIs o stores autoritativos").
 */

export type NeighborNodeType = 'requirement' | 'decision' | 'symbol' | 'test' | 'run' | 'path';

/** `relation` is never guessed by this adapter -- it is exactly what the
 *  server computed: `declared` (a human/agent recorded the link on purpose),
 *  `located` (found mechanically, by the code index or a text search) or
 *  `verified` (an `evidences` link resolves against the requirement's
 *  CURRENT revision -- see `knowledge_neighborhood.py`'s own module
 *  docstring for why this is the only path to that word). */
export type NeighborRelation = 'declared' | 'located' | 'verified';

export interface NeighborNode {
  id: string;
  type: NeighborNodeType;
  label: string;
  ref: string;
  /** Why this node is here -- rendered as-is; the server already writes it
   *  as a short, human sentence (never a code the UI has to translate). */
  why: string;
  stale: boolean;
  status?: string;
  verified?: boolean;
  implemented?: boolean;
  tested?: boolean;
}

export interface NeighborEdge {
  src: string;
  dst: string;
  relation: NeighborRelation;
  kind: string;
  stale: boolean;
  why: string;
}

export interface Neighborhood {
  projectId: string;
  path: string | null;
  reqKey: string | null;
  issueKey: string | null;
  depth: number;
  nodes: NeighborNode[];
  edges: NeighborEdge[];
  /** Requested `req_key`/`issue_key` ids that do not exist -- never
   *  answered with a fabricated neighborhood for an id that was not found. */
  unknown: string[];
  /** Every node ref whose evidence is out of date, deduplicated -- the
   *  headline for "what quedó desactualizado" without re-walking `nodes`. */
  staleRefs: string[];
}

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function bool(value: unknown): boolean {
  return value === true;
}

function nodeFrom(raw: Record<string, unknown>): NeighborNode {
  const type = str(raw.type, 'symbol');
  return {
    id: str(raw.id),
    type: (['requirement', 'decision', 'symbol', 'test', 'run', 'path'].includes(type) ? type : 'symbol') as NeighborNodeType,
    label: str(raw.label) || str(raw.id),
    ref: str(raw.ref),
    why: str(raw.why),
    stale: bool(raw.stale),
    status: str(raw.status) || undefined,
    verified: typeof raw.verified === 'boolean' ? raw.verified : undefined,
    implemented: typeof raw.implemented === 'boolean' ? raw.implemented : undefined,
    tested: typeof raw.tested === 'boolean' ? raw.tested : undefined,
  };
}

function edgeFrom(raw: Record<string, unknown>): NeighborEdge {
  const relation = str(raw.relation, 'located');
  return {
    src: str(raw.src),
    dst: str(raw.dst),
    relation: (['declared', 'located', 'verified'].includes(relation) ? relation : 'located') as NeighborRelation,
    kind: str(raw.kind),
    stale: bool(raw.stale),
    why: str(raw.why),
  };
}

export async function getNeighborhood(
  projectId: string,
  opts: { path?: string; reqKey?: string; issueKey?: string; depth?: number } = {},
  signal?: AbortSignal,
): Promise<Neighborhood> {
  const params = new URLSearchParams();
  if (opts.path) params.set('path', opts.path);
  if (opts.reqKey) params.set('req_key', opts.reqKey);
  if (opts.issueKey) params.set('issue_key', opts.issueKey);
  if (opts.depth) params.set('depth', String(opts.depth));
  const query = params.toString();
  const data = await getJson<{ neighborhood: Record<string, unknown> }>(
    `/api/projects/${encodeURIComponent(projectId)}/knowledge/neighborhood${query ? `?${query}` : ''}`,
    signal,
  );
  const raw = data.neighborhood || {};
  const nodes = Array.isArray(raw.nodes) ? (raw.nodes as Record<string, unknown>[]).map(nodeFrom) : [];
  const edges = Array.isArray(raw.edges) ? (raw.edges as Record<string, unknown>[]).map(edgeFrom) : [];
  return {
    projectId: str(raw.project_id, projectId),
    path: str(raw.path) || null,
    reqKey: str(raw.req_key) || null,
    issueKey: str(raw.issue_key) || null,
    depth: typeof raw.depth === 'number' ? raw.depth : 1,
    nodes,
    edges,
    unknown: Array.isArray(raw.unknown) ? (raw.unknown as unknown[]).map(String) : [],
    staleRefs: Array.isArray(raw.stale_refs) ? (raw.stale_refs as unknown[]).map(String) : [],
  };
}
