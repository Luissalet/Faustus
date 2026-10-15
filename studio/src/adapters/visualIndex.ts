import { ApiError, CLIENT_API_VERSION, CLIENT_VERSION_HEADER, getJson, responseReason } from './api';

/**
 * Local visual index (`/api/visual-index/*`) shaped for Studio.
 *
 * A strip is a piece of a page (at most 1024 px high), not its text. The screen never says a strip "contains"
 * something it has not read: the reading is a separate, explicit call, and when no vision model answers the
 * server says so and nothing is drawn in its place.
 */

const str = (value: unknown): string => (typeof value === 'string' ? value : '');
const num = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? value : 0);

function obj(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

async function sendJson(method: string, path: string, body?: unknown): Promise<unknown> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION,
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  const text = await response.text();
  return text ? JSON.parse(text) : null;
}

export type CollectionSummary = { name: string; sources: number; strips: number; embedder: string };
export type SourceRow = { source: string; kind: string; title: string; strips: number; pages: number; complete: boolean };
export type Hit = {
  id: string;
  collection: string;
  source: string;
  kind: string;
  title: string;
  page: number;
  yOffset: number;
  width: number;
  height: number;
  score: number;
};
export type IndexStatus = {
  enabled: boolean;
  embedderName: string;
  dependenciesMissing: string[];
  installHint: string;
  modelDownloaded: boolean;
  loaded: boolean;
  device: string;
  visionAvailable: boolean;
  visionChecking: boolean;
  visionModel: string;
  visionNote: string;
  collections: CollectionSummary[];
};
export type AddRow = { source: string; ok: boolean; unchanged: boolean; added: number; reused: number; removed: number; total: number; note: string };
export type ReadResult = { answeredBy: string; visionAvailable: boolean; text: string };

export function collectionFrom(raw: unknown): CollectionSummary {
  const o = obj(raw);
  return { name: str(o.collection) || str(o.name), sources: num(o.sources), strips: num(o.strips), embedder: str(o.embedder) };
}

export function hitFrom(raw: unknown): Hit {
  const o = obj(raw);
  return {
    id: str(o.id),
    collection: str(o.collection),
    source: str(o.source),
    kind: str(o.kind),
    title: str(o.title),
    page: num(o.page) || 1,
    yOffset: num(o.y_offset),
    width: num(o.width),
    height: num(o.height),
    score: num(o.score),
  };
}

export function statusFrom(raw: unknown): IndexStatus {
  const o = obj(raw);
  const emb = obj(o.embedder);
  const vis = obj(o.vision);
  const missing = Array.isArray(emb.dependencies_missing) ? emb.dependencies_missing.map(String) : [];
  return {
    enabled: o.enabled !== false,
    embedderName: str(emb.embedder),
    dependenciesMissing: missing,
    installHint: str(emb.install_hint),
    modelDownloaded: emb.model_downloaded === true,
    loaded: emb.loaded === true,
    device: str(emb.device),
    visionAvailable: vis.available === true,
    visionChecking: vis.checking === true,
    visionModel: str(vis.model),
    visionNote: str(vis.note),
    collections: (Array.isArray(o.collections) ? o.collections : []).map(collectionFrom),
  };
}

/** What the person needs to know before indexing: ready, or the one thing missing. Never claims more than the status says. */
export function readiness(status: IndexStatus): { ready: boolean; reason: string } {
  if (!status.enabled) return { ready: false, reason: 'off' };
  if (status.dependenciesMissing.length) return { ready: false, reason: 'dependencies' };
  return { ready: true, reason: status.modelDownloaded || status.loaded ? 'ready' : 'download' };
}

/** One source per line (commas also split, so a pasted list works); duplicates and blanks dropped, order kept. */
export function parseSources(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.split(/[\n,]+/)) {
    const item = raw.trim().replace(/^["']|["']$/g, '');
    if (item && !seen.has(item)) {
      seen.add(item);
      out.push(item);
    }
  }
  return out;
}

export function isUrl(source: string): boolean {
  return /^https?:\/\//i.test(source.trim());
}

/** "p. 3 · y 1024 px" for a PDF, "y 2048 px" for a page capture, "" for a picture that fits one strip. */
export function hitLocation(hit: Hit): string {
  const parts: string[] = [];
  if (hit.kind === 'pdf') parts.push(`p. ${hit.page}`);
  if (hit.yOffset > 0 || hit.kind === 'web') parts.push(`y ${hit.yOffset} px`);
  return parts.join(' · ');
}

export function shortSource(source: string, max = 64): string {
  if (source.length <= max) return source;
  const tail = source.slice(-(max - 1));
  return `…${tail}`;
}

export function tileImageUrl(id: string): string {
  return `/api/visual-index/tiles/${encodeURIComponent(id)}/image`;
}

export async function loadStatus(): Promise<IndexStatus> {
  return statusFrom(await getJson<unknown>('/api/visual-index/status'));
}

export async function listCollections(): Promise<CollectionSummary[]> {
  const data = obj(await getJson<unknown>('/api/visual-index/collections'));
  return (Array.isArray(data.collections) ? data.collections : []).map(collectionFrom);
}

export async function listSources(collection: string): Promise<SourceRow[]> {
  const data = obj(await getJson<unknown>(`/api/visual-index/collections/${encodeURIComponent(collection)}`));
  return (Array.isArray(data.sources) ? data.sources : []).map((raw) => {
    const o = obj(raw);
    return { source: str(o.source), kind: str(o.kind), title: str(o.title), strips: num(o.strips), pages: num(o.pages), complete: o.complete !== false };
  });
}

export async function addSources(sources: string[], collection: string, pages: string): Promise<AddRow[]> {
  const body: Record<string, unknown> = { sources, collection: collection || 'default' };
  if (pages.trim()) body.pages = pages.trim();
  const data = obj(await sendJson('POST', '/api/visual-index/add', body));
  return (Array.isArray(data.results) ? data.results : []).map((raw) => {
    const o = obj(raw);
    const notes = Array.isArray(o.notes) ? o.notes.map(String).join('; ') : '';
    return {
      source: str(o.source),
      ok: o.ok === true,
      unchanged: o.unchanged === true,
      added: num(o.added),
      reused: num(o.reused),
      removed: num(o.removed),
      total: num(o.total),
      note: o.ok === true ? notes : str(o.error),
    };
  });
}

export async function search(query: string, k: number, collection: string): Promise<Hit[]> {
  const body: Record<string, unknown> = { query, k };
  if (collection) body.collection = collection;
  const data = obj(await sendJson('POST', '/api/visual-index/search', body));
  return (Array.isArray(data.results) ? data.results : []).map(hitFrom);
}

export async function readTile(id: string, question: string): Promise<ReadResult> {
  const data = obj(await sendJson('POST', `/api/visual-index/tiles/${encodeURIComponent(id)}/read`, { question }));
  return { answeredBy: str(data.answered_by), visionAvailable: data.vision_available !== false && str(data.answered_by) !== 'none', text: str(data.text) };
}

export async function removeSource(collection: string, source: string): Promise<void> {
  await sendJson('DELETE', `/api/visual-index/collections/${encodeURIComponent(collection)}?source=${encodeURIComponent(source)}`);
}

export async function removeCollection(collection: string): Promise<void> {
  await sendJson('DELETE', `/api/visual-index/collections/${encodeURIComponent(collection)}`);
}
