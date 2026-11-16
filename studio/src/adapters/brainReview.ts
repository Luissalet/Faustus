import { ApiError, asArray, getJson } from './api';

/**
 * Ratified facts, contradictions, expiry and the galaxy view of the brain
 * (`/api/brain/review`, `/ratify`, `/review/resolve`, `/galaxy`). Same
 * discipline as `adapters/brain.ts`: every reader tolerates a missing field.
 */

const BASE = '/api/brain';

const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback);
const num = (v: unknown, fallback = 0): number => (typeof v === 'number' && Number.isFinite(v) ? v : fallback);
const bool = (v: unknown): boolean => v === true;
const strArray = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);
const obj = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {});

async function post(path: string, body: unknown): Promise<unknown> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = '';
    try {
      const raw = (await response.clone().json()) as { detail?: unknown };
      if (typeof raw.detail === 'string') detail = raw.detail;
    } catch {
      /* the status line is all there is */
    }
    throw new ApiError(detail || `${path} responded ${response.status}`, response.status);
  }
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/* ── review ──────────────────────────────────────────────────────────── */

export interface ReviewOption {
  ref: string;
  relationId: string;
  value: string;
  validFrom: string;
  validUntil: string;
  evidence: string[];
  confidence: number;
  method: string;
  ratified: boolean;
}

export interface Contradiction {
  id: string;
  entity: { id: string; name: string; type: string };
  rel: string;
  options: ReviewOption[];
  settledByRatification: boolean;
  ratifiedRef: string;
}

export interface ExpiryItem {
  id: string;
  kind: 'fact' | 'note';
  ref: string;
  state: 'expired' | 'soon';
  expiresAt: string;
  title: string;
  path: string;
  entityId: string;
  ratified: boolean;
}

export interface ReviewCounts {
  contradictions: number;
  unsettled: number;
  expired: number;
  soon: number;
  ratified: number;
}

export interface Review {
  contradictions: Contradiction[];
  expired: ExpiryItem[];
  counts: ReviewCounts;
}

function optionFrom(raw: unknown): ReviewOption {
  const r = obj(raw);
  return {
    ref: str(r.ref),
    relationId: str(r.relation_id),
    value: str(r.value),
    validFrom: str(r.valid_from),
    validUntil: str(r.valid_until),
    evidence: strArray(r.evidence),
    confidence: num(r.confidence, 1),
    method: str(r.method, 'rule'),
    ratified: bool(r.ratified),
  };
}

function contradictionFrom(raw: unknown): Contradiction {
  const r = obj(raw);
  const e = obj(r.entity);
  return {
    id: str(r.id),
    entity: { id: str(e.id), name: str(e.name), type: str(e.type, 'other') },
    rel: str(r.rel),
    options: asArray<unknown>(r.options).map(optionFrom),
    settledByRatification: bool(r.settled_by_ratification),
    ratifiedRef: str(r.ratified_ref),
  };
}

function expiryFrom(raw: unknown): ExpiryItem {
  const r = obj(raw);
  return {
    id: str(r.id),
    kind: r.kind === 'note' ? 'note' : 'fact',
    ref: str(r.ref),
    state: r.state === 'soon' ? 'soon' : 'expired',
    expiresAt: str(r.expires_at),
    title: str(r.title),
    path: str(r.path),
    entityId: str(obj(r.entity).id),
    ratified: bool(r.ratified),
  };
}

export function reviewFrom(raw: unknown): Review {
  const r = obj(raw);
  const c = obj(r.counts);
  const contradictions = asArray<unknown>(r.contradictions).map(contradictionFrom);
  const expired = asArray<unknown>(r.expired).map(expiryFrom);
  return {
    contradictions,
    expired,
    counts: {
      contradictions: num(c.contradictions, contradictions.length),
      unsettled: num(c.unsettled),
      expired: num(c.expired, expired.filter((x) => x.state === 'expired').length),
      soon: num(c.soon, expired.filter((x) => x.state === 'soon').length),
      ratified: num(c.ratified),
    },
  };
}

export function loadReview(signal?: AbortSignal): Promise<Review> {
  return getJson<unknown>(`${BASE}/review`, signal).then(reviewFrom);
}

export async function setRatified(ref: string, on: boolean, note = ''): Promise<void> {
  await post(`${BASE}/ratify`, on ? { ref, note } : { ref, off: true });
}

export type ReviewAction = 'keep' | 'archive' | 'update' | 'renew';

export interface ResolveInput {
  action: ReviewAction;
  ref: string;
  value?: string;
  until?: string;
}

export async function resolveReview(input: ResolveInput): Promise<{ changed: string[] }> {
  const body: Record<string, unknown> = { action: input.action, ref: input.ref };
  if (input.value !== undefined) body.value = input.value;
  if (input.until !== undefined) body.until = input.until;
  const raw = obj(await post(`${BASE}/review/resolve`, body));
  return { changed: strArray(raw.changed) };
}

/* ── galaxy ──────────────────────────────────────────────────────────── */

export type GalaxyKind = 'project' | 'agent' | 'session' | 'output' | 'entity';
export const GALAXY_KINDS: GalaxyKind[] = ['project', 'agent', 'session', 'output', 'entity'];

export interface GalaxyNode {
  id: string;
  kind: GalaxyKind;
  label: string;
  pos: [number, number, number];
  size: number;
  ratified: boolean;
  /** `ent:<id>` for an entity, else ''. */
  ref: string;
}

export interface Galaxy {
  nodes: GalaxyNode[];
  /** Pairs of indexes into `nodes`. */
  links: [number, number][];
  counts: Record<GalaxyKind, number>;
  warnings: string[];
  radius: number;
}

function kindOf(v: unknown): GalaxyKind {
  return GALAXY_KINDS.includes(v as GalaxyKind) ? (v as GalaxyKind) : 'entity';
}

export function galaxyFrom(raw: unknown): Galaxy {
  const r = obj(raw);
  const nodes: GalaxyNode[] = asArray<unknown>(r.nodes).map((item) => {
    const n = obj(item);
    const p = Array.isArray(n.pos) ? n.pos : [];
    return {
      id: str(n.id),
      kind: kindOf(n.kind),
      label: str(n.label),
      pos: [num(p[0]), num(p[1]), num(p[2])],
      size: num(n.size, 3),
      ratified: bool(n.ratified),
      ref: str(n.ref),
    };
  });
  const index = new Map(nodes.map((n, i) => [n.id, i] as const));
  const links: [number, number][] = [];
  for (const pair of asArray<unknown>(r.links)) {
    if (!Array.isArray(pair)) continue;
    const a = index.get(str(pair[0]));
    const b = index.get(str(pair[1]));
    if (a !== undefined && b !== undefined) links.push([a, b]);
  }
  const c = obj(r.counts);
  const counts = Object.fromEntries(GALAXY_KINDS.map((k) => [k, num(c[k])])) as Record<GalaxyKind, number>;
  return { nodes, links, counts, warnings: strArray(r.warnings), radius: num(r.radius, 2) || 2 };
}

export function loadGalaxy(limit = 300, signal?: AbortSignal): Promise<Galaxy> {
  return getJson<unknown>(`${BASE}/galaxy?limit=${encodeURIComponent(String(limit))}`, signal).then(galaxyFrom);
}
