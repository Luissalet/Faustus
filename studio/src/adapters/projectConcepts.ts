import { ApiError, getJson } from './api';

/**
 * Project concepts (`/api/project-concepts`) — the agent's own persistent,
 * per-project graph of architecture concepts (src/project_concepts.py).
 *
 * Scoped by `workspace` (or `projectId` when known) on every call, the same
 * way `adapters/context.ts`'s code-index calls are — a concept never leaks
 * across projects because every request carries the scope explicitly.
 */

const BASE = '/api/project-concepts';

export const CONCEPT_KINDS = ['feature', 'module', 'pattern', 'config', 'decision', 'component'] as const;
export type ConceptKind = (typeof CONCEPT_KINDS)[number];

export const CONCEPT_RELATIONS = ['connects_to', 'depends_on', 'implements', 'calls', 'configured_by'] as const;
export type ConceptRelation = (typeof CONCEPT_RELATIONS)[number];

export interface ConceptEdge {
  id: string;
  src: string;
  dst: string;
  rel: string;
  note: string;
}

export interface Concept {
  id: string;
  name: string;
  kind: string;
  summary: string;
  details: string;
  refs: string[];
  parent_id: string | null;
  created_at: string;
  updated_at: string;
  removed_at: string | null;
  child_count?: number;
  degree?: number;
  score?: number;
}

export interface ConceptDetail extends Concept {
  incoming: ConceptEdge[];
  outgoing: ConceptEdge[];
  children: Concept[];
}

export interface ConceptGraph {
  nodes: Concept[];
  edges: ConceptEdge[];
}

export interface UnderstandResult {
  query: string;
  concepts: Concept[];
  neighbors: Concept[];
}

export interface StaleResult {
  stale: boolean;
  checked: boolean;
  issues: Array<{ ref: string; why: string }>;
}

export interface HistoryEntry {
  id: string;
  concept_id: string;
  ts: string;
  action: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

function query(params: Record<string, string | number | undefined>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    const text = value === undefined ? '' : String(value);
    if (text) parts.push(`${key}=${encodeURIComponent(text)}`);
  }
  return parts.length ? `?${parts.join('&')}` : '';
}

async function send(path: string, method: 'POST' | 'DELETE', body?: unknown): Promise<Record<string, unknown>> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers:
      body === undefined
        ? { Accept: 'application/json' }
        : { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = '';
    try {
      const raw = (await response.clone().json()) as { detail?: unknown };
      if (typeof raw.detail === 'string') detail = raw.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail || `${path} responded ${response.status}`, response.status);
  }
  try {
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

export async function loadRoots(scope: { workspace?: string; projectId?: string }, signal?: AbortSignal): Promise<Concept[]> {
  const data = await getJson<{ concepts: Concept[] }>(
    `${BASE}${query({ workspace: scope.workspace, project_id: scope.projectId })}`,
    signal,
  );
  return data.concepts ?? [];
}

export async function loadGraph(scope: { workspace?: string; projectId?: string }, signal?: AbortSignal): Promise<ConceptGraph> {
  return getJson<ConceptGraph>(`${BASE}/graph${query({ workspace: scope.workspace, project_id: scope.projectId })}`, signal);
}

export async function understand(
  q: string,
  scope: { workspace?: string; projectId?: string },
  k = 6,
  signal?: AbortSignal,
): Promise<UnderstandResult> {
  return getJson<UnderstandResult>(
    `${BASE}/understand${query({ q, k, workspace: scope.workspace, project_id: scope.projectId })}`,
    signal,
  );
}

export async function loadConcept(
  id: string,
  scope: { workspace?: string; projectId?: string },
  signal?: AbortSignal,
): Promise<ConceptDetail> {
  const data = await getJson<{ concept: ConceptDetail }>(
    `${BASE}/${encodeURIComponent(id)}${query({ workspace: scope.workspace, project_id: scope.projectId })}`,
    signal,
  );
  return data.concept;
}

export async function loadHistory(
  id: string,
  scope: { workspace?: string; projectId?: string },
  signal?: AbortSignal,
): Promise<HistoryEntry[]> {
  const data = await getJson<{ history: HistoryEntry[] }>(
    `${BASE}/${encodeURIComponent(id)}/history${query({ workspace: scope.workspace, project_id: scope.projectId })}`,
    signal,
  );
  return data.history ?? [];
}

export async function loadStale(
  id: string,
  scope: { workspace?: string; projectId?: string },
  signal?: AbortSignal,
): Promise<StaleResult> {
  return getJson<StaleResult>(
    `${BASE}/${encodeURIComponent(id)}/stale${query({ workspace: scope.workspace, project_id: scope.projectId })}`,
    signal,
  );
}

export interface ConceptDraft {
  id?: string;
  name: string;
  kind: ConceptKind;
  summary?: string;
  details?: string;
  refs?: string[];
  parent_id?: string;
}

export async function upsertConcept(
  draft: ConceptDraft,
  scope: { workspace?: string; projectId?: string },
): Promise<Concept> {
  const data = await send(BASE, 'POST', { ...draft, workspace: scope.workspace, project_id: scope.projectId });
  return data.concept as Concept;
}

export async function linkConcepts(
  src: string,
  dst: string,
  rel: ConceptRelation,
  note: string,
  scope: { workspace?: string; projectId?: string },
): Promise<ConceptEdge> {
  const data = await send(`${BASE}/link`, 'POST', {
    src, dst, rel, note, workspace: scope.workspace, project_id: scope.projectId,
  });
  return data.edge as ConceptEdge;
}

export async function removeConcept(id: string, scope: { workspace?: string; projectId?: string }): Promise<boolean> {
  await send(`${BASE}/${encodeURIComponent(id)}${query({ workspace: scope.workspace, project_id: scope.projectId })}`, 'DELETE');
  return true;
}
