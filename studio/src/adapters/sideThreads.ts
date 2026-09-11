import { ApiError, responseReason } from './api';
import { t, tn } from '../i18n';

/**
 * Lote B (CONTRATO_EXCURSOS.md) — Excursos (side threads), a thin typed
 * mirror of Lote A's backend: `src/side_threads.py` / `routes/side_thread_
 * routes.py` (request/response shapes) and `docs/api/side_threads.md`
 * (the contract this file must never drift from). Field names are kept
 * exactly as the server writes them — the same choice `adapters/
 * requirements.ts` made — so this adapter never re-implements a rule the
 * server already owns: anchor validation, staleness, the depth caps, all
 * stay server-side.
 *
 * Errors: every route here answers a failure with the FLAT
 * `{"error": str, "error_class": "excursos.<motivo>"}` body CONTRATO_
 * EXCURSOS requires — note the message lives under `error`, not FastAPI's
 * usual `detail`, so `request()` below reads it directly rather than
 * through `adapters/api.ts`'s `responseReason` (kept only as the fallback
 * for a response this module did not itself shape, e.g. a raw 422 from
 * FastAPI's own request validation, which does use `detail`).
 */

export type WireKind = 'branch' | 'reference';
export type ReferenceDepth = 'quote' | 'full';
export type AnchorState = 'ok' | 'missing';

export interface Wire {
  id: string;
  kind: WireKind;
  owner: string | null;
  source_session_id: string;
  target_session_id: string;
  anchor_index: number | null;
  anchor_passage: string | null;
  depth: ReferenceDepth;
  context_order: number;
  archived: boolean;
  source_fingerprint: string | null;
  created_at: string | null;
}

export interface ParentInfo {
  wire: Wire;
  session: { id: string; name: string };
  anchor_state: AnchorState;
}

export interface ChildInfo {
  wire: Wire;
  session: { id: string; name: string; message_count: number };
  reference: Wire | null;
  stale: boolean;
}

export interface ReferenceInInfo {
  wire: Wire;
  session: { id: string; name: string };
  stale: boolean;
  tokens: number;
}

export interface ReferenceOutInfo {
  wire: Wire;
  session: { id: string; name: string };
}

export interface WiresFor {
  parent: ParentInfo | null;
  children: ChildInfo[];
  references_in: ReferenceInInfo[];
  references_out: ReferenceOutInfo[];
}

export interface ThoughtMapNode {
  id: string;
  name: string;
  message_count: number;
  anchor_index: number | null;
  children: ThoughtMapNode[];
  current?: boolean;
}

/** `thought_map` hands back the root node with the requested session marked
 *  `current`, plus `truncated` when either walk (up to the root, or back
 *  down) hit `MAX_THOUGHT_MAP_DEPTH`. In the corrupted-data edge case
 *  `src/side_threads.py::thought_map` documents (its own root lookup
 *  returning nothing) the body is `{}` — callers check `id` before
 *  rendering a node rather than assume one is always there. */
export type ThoughtMap = Partial<ThoughtMapNode> & { truncated?: boolean };

export interface ReferenceLayerItem {
  session_id: string;
  name: string;
  depth: ReferenceDepth;
  stale: boolean;
}

export interface ReferencesLayer {
  layer: 'references';
  messages: number;
  tokens: number;
  items: ReferenceLayerItem[];
}

export interface InheritedFrom {
  session_id: string;
  name: string;
  anchor_index: number | null;
  anchor_state: AnchorState;
}

export interface InheritedLayer {
  layer: 'inherited';
  messages: number;
  tokens: number;
  from: InheritedFrom | null;
}

export interface OwnLayer {
  layer: 'own';
  messages: number;
  tokens: number;
}

export type ContextLayer = ReferencesLayer | InheritedLayer | OwnLayer;

export interface ContextPreview {
  layers: ContextLayer[];
  total_tokens: number;
}

export class SideThreadsApiError extends ApiError {
  readonly errorClass: string | null;
  readonly payload: Record<string, unknown>;

  constructor(message: string, status: number, errorClass: string | null, payload: Record<string, unknown>) {
    super(message, status);
    this.name = 'SideThreadsApiError';
    this.errorClass = errorClass;
    this.payload = payload;
  }
}

async function payloadOf(response: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await response.clone().json();
    return body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const payload = await payloadOf(response);
    const errorClass = typeof payload.error_class === 'string' ? payload.error_class : null;
    const flatMessage = typeof payload.error === 'string' && payload.error.trim() ? payload.error : null;
    const message = flatMessage ?? (await responseReason(response, path));
    throw new SideThreadsApiError(message, response.status, errorClass, payload);
  }
  return (await response.json()) as T;
}

function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
}

function patchJson<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
}

const sessionBase = (id: string) => `/api/session/${encodeURIComponent(id)}`;

// ---------------------------------------------------------------------------
// Wire mutation / retrieval
// ---------------------------------------------------------------------------

export interface CreateSideThreadInput {
  anchorIndex: number;
  passage?: string;
  question?: string;
}

export interface CreateSideThreadResult {
  session_id: string;
  wire: Wire;
  question: string | null;
}

/** `POST .../side-threads` — a brand-new session wired to `sessionId` by a
 *  `branch` cable; never copies a message (unlike `/fork`). `question`, if
 *  given, comes back unchanged for the caller to seed the new session's
 *  composer with — it is never sent to any model by this route. */
export function createSideThread(sessionId: string, input: CreateSideThreadInput): Promise<CreateSideThreadResult> {
  return post(`${sessionBase(sessionId)}/side-threads`, {
    anchor_index: input.anchorIndex,
    passage: input.passage,
    question: input.question,
  });
}

export function getWires(sessionId: string, signal?: AbortSignal): Promise<WiresFor> {
  return get(`${sessionBase(sessionId)}/side-threads`, signal);
}

export function getThoughtMap(sessionId: string, signal?: AbortSignal): Promise<ThoughtMap> {
  return get(`${sessionBase(sessionId)}/thought-map`, signal);
}

export function getContextPreview(sessionId: string, signal?: AbortSignal): Promise<ContextPreview> {
  return get(`${sessionBase(sessionId)}/context-preview`, signal);
}

export interface AddReferenceInput {
  sourceSessionId: string;
  depth?: ReferenceDepth;
}

/** `POST .../references` — "Traer de vuelta": wires a side thread back into
 *  `targetId` as one `[Reference: ...]` block. Idempotent server-side:
 *  cabling the same pair twice returns the existing wire rather than a
 *  second one. */
export function addReference(targetId: string, input: AddReferenceInput): Promise<{ wire: Wire; tokens: number }> {
  return post(`${sessionBase(targetId)}/references`, { source_session_id: input.sourceSessionId, depth: input.depth });
}

export interface UpdateReferenceInput {
  depth?: ReferenceDepth;
  archived?: boolean;
  contextOrder?: number;
  refresh?: boolean;
}

export function updateReference(targetId: string, wireId: string, patch: UpdateReferenceInput): Promise<{ wire: Wire }> {
  return patchJson(`${sessionBase(targetId)}/references/${encodeURIComponent(wireId)}`, {
    depth: patch.depth,
    archived: patch.archived,
    context_order: patch.contextOrder,
    refresh: patch.refresh,
  });
}

/** DELETE physically removes the `reference` wire (never the side thread
 *  it pointed at). This is the ONLY `method: 'DELETE'` call in this
 *  adapter, and it only ever targets a `/references/{id}` path —
 *  `studio/checks/side_threads.check.mjs` greps this file for exactly
 *  that invariant, so a future edit that adds another DELETE elsewhere in
 *  this module (e.g. against a bare session) would fail the check. */
export function removeReference(targetId: string, wireId: string): Promise<{ removed: true }> {
  return request(`${sessionBase(targetId)}/references/${encodeURIComponent(wireId)}`, { method: 'DELETE' });
}

export function getParentsMap(signal?: AbortSignal): Promise<{ parents: Record<string, string> }> {
  return get('/api/side-threads/parents', signal);
}

// ---------------------------------------------------------------------------
// Pure presentation helpers — exercised by studio/checks/side_threads.check.mjs
// without a DOM.
// ---------------------------------------------------------------------------

/** "1.2k" for four digits and up, the plain number below — the digits half
 *  of the contract's own example ("Enviará ~1.2k tok …"; the "~" is the
 *  caller's to add, see `layerSummaryLine`). Never negative, never more
 *  than one decimal. */
export function formatTokenCount(n: number): string {
  const count = Math.max(0, Math.round(n));
  if (count < 1000) return String(count);
  const k = count / 1000;
  const rounded = k >= 10 ? Math.round(k) : Math.round(k * 10) / 10;
  return `${rounded}k`;
}

/** "Qué verá el modelo" summary line, e.g. "Enviará ~1.2k tok · 14 mensajes
 *  (3 heredados, 1 referencia)" — the contract's own example. Pure: reads
 *  only `preview.layers`/`total_tokens`, so it renders identically whether
 *  it is called from the panel or from this file's own check. */
export function layerSummaryLine(preview: ContextPreview): string {
  const references = preview.layers.find((l): l is ReferencesLayer => l.layer === 'references');
  const inherited = preview.layers.find((l): l is InheritedLayer => l.layer === 'inherited');
  const own = preview.layers.find((l): l is OwnLayer => l.layer === 'own');
  const totalMessages = (references?.messages ?? 0) + (inherited?.messages ?? 0) + (own?.messages ?? 0);
  const inheritedCount = inherited?.messages ?? 0;
  const referenceCount = references?.items.length ?? 0;

  const bits: string[] = [];
  if (inheritedCount > 0) bits.push(tn(inheritedCount, '{n} inherited', '{n} inherited#', { n: inheritedCount }));
  if (referenceCount > 0) bits.push(tn(referenceCount, '{n} reference', '{n} references', { n: referenceCount }));
  const detail = bits.length ? ` (${bits.join(', ')})` : '';
  const messages = tn(totalMessages, '{n} message', '{n} messages', { n: totalMessages });

  return t('Will send ~{tokens} tok · {messages}{detail}', {
    tokens: formatTokenCount(preview.total_tokens),
    messages,
    detail,
  });
}

/** Mirrors `src/side_threads.py::create_side_thread`'s own naming
 *  ("↳ " + the first 48 chars of the passage, or the question, or the
 *  parent's name) — used to preview a side thread's name before it exists
 *  (`ExploreDialog`), never to rename one after the server has assigned it. */
export function sideThreadName(passage: string | null | undefined, question: string | null | undefined, parentName: string): string {
  const base = (passage && passage.trim()) || (question && question.trim()) || parentName || '';
  return `↳ ${base.slice(0, 48)}`;
}

/** "desde el turno N" / "el turno de origen ya no existe" — `anchorIndex`
 *  is the 0-based position in the parent's `history`
 *  (`src/side_threads.py::create_side_thread`'s own `anchor_index`); shown
 *  to a person as 1-based, "turn N". */
export function formatAnchor(anchorIndex: number | null | undefined, anchorState: AnchorState): string {
  if (anchorState === 'missing') return t('the source turn no longer exists');
  const n = typeof anchorIndex === 'number' ? anchorIndex + 1 : null;
  return n === null ? t('from an earlier turn') : t('from turn {n}', { n });
}

export interface IndentedSession<T> {
  item: T;
  depth: number;
}

const MAX_INDENT_DEPTH = 8;

/** SessionsPane.tsx: reorders `list` so a session whose PARENT is also in
 *  `list` (per `parents`, `getParentsMap()`'s `{child: parent}`) is drawn
 *  directly after its parent, `depth` counting how many such ancestors are
 *  in `list` too. A session whose parent is absent from `list` (a
 *  different sort/filter/search, or simply not a side thread) is left
 *  exactly where the caller's own sort put it, at depth 0 — this never
 *  reaches into the network or blocks on `parents` being complete
 *  (SessionsPane.tsx: "si la petición falla, la lista se pinta como
 *  hoy"). Cycle-safe (a `seen` set plus a depth cap) even though the
 *  server's own invariant already rules a cycle out. */
export function indentSessions<T extends { id: string }>(list: T[], parents: Record<string, string>): IndentedSession<T>[] {
  const byId = new Map(list.map((item) => [item.id, item]));
  const childrenOf = new Map<string, T[]>();
  const isChildHere = new Set<string>();
  for (const item of list) {
    const parentId = parents[item.id];
    if (parentId && parentId !== item.id && byId.has(parentId)) {
      isChildHere.add(item.id);
      const siblings = childrenOf.get(parentId) ?? [];
      siblings.push(item);
      childrenOf.set(parentId, siblings);
    }
  }

  const out: IndentedSession<T>[] = [];
  const seen = new Set<string>();
  const emit = (item: T, depth: number) => {
    if (seen.has(item.id) || depth > MAX_INDENT_DEPTH) return;
    seen.add(item.id);
    out.push({ item, depth });
    for (const child of childrenOf.get(item.id) ?? []) emit(child, depth + 1);
  };
  for (const item of list) {
    if (!isChildHere.has(item.id)) emit(item, 0);
  }
  return out;
}
