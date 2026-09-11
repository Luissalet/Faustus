import { ApiError, responseReason } from './api';

/**
 * W4-A — Requirements (`/api/projects/{project_id}/requirements/*`,
 * `src/requirements/{store,context,evidence}.py`). See
 * `docs/api/requirements.md` and `docs/requirements-format.md` for the full
 * contract; this adapter is a thin, typed mirror of the wire shapes (field
 * names kept exactly as the server writes them, the same choice
 * `adapters/alternatives.ts` made) — it never re-implements a rule the
 * server already owns.
 *
 * The one rule this file itself upholds rather than merely reflecting:
 * every write goes through `updateRequirement`, which always sends
 * `by: 'human'`. There is no path in this adapter that can send
 * `by: 'model'` — accepting or rejecting a requirement is a decision only a
 * person makes in this UI (the server enforces the same rule independently
 * in `Store.update`, `requirements.model_cannot_decide`; this is belt, the
 * server is suspenders).
 */

export type RequirementSource = 'doc' | 'issue' | 'url' | 'human';
export type RequirementStatus = 'proposed' | 'accepted' | 'rejected' | 'superseded';
export type ProposedBy = 'human' | 'model';
export type LinkKind = 'implements' | 'tests' | 'evidences' | 'issue';
export type LinkState = 'linked' | 'needs_review' | 'stale' | 'unknown';

export const REQUIREMENT_STATUSES: RequirementStatus[] = ['proposed', 'accepted', 'rejected', 'superseded'];
export const REQUIREMENT_SOURCES: RequirementSource[] = ['doc', 'issue', 'url', 'human'];
export const LINK_KINDS: LinkKind[] = ['implements', 'tests', 'evidences', 'issue'];

export interface RequirementLink {
  id: string;
  req_id: string;
  kind: LinkKind;
  target: string;
  revision: string;
  req_revision_at_link: number;
  content_hash: string | null;
  state: LinkState;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface Requirement {
  id: string;
  project_id: string;
  key: string;
  title: string;
  text: string;
  source: RequirementSource;
  acceptance: string[];
  status: RequirementStatus;
  proposed_by: ProposedBy;
  current_revision: number;
  created_at: string;
  updated_at: string;
  created_by: string;
  links: RequirementLink[];
}

export interface RequirementRevision {
  id: string;
  req_id: string;
  revision: number;
  title: string;
  text: string;
  source: RequirementSource;
  acceptance: string[];
  status: RequirementStatus;
  changed_by: string;
  change_note: string;
  created_at: string;
}

/** A link row as `GET .../matrix` recomputes it live — `live_state`/
 *  `live_meta` are what was just measured, distinct from `state`, the value
 *  last persisted (see `docs/api/requirements.md#matriz-de-cobertura`: the
 *  matrix route is the only reader allowed to write `state` back). */
export interface MatrixLink extends RequirementLink {
  live_state: LinkState;
  live_meta: Record<string, unknown>;
}

export interface MatrixRow {
  key: string;
  project_id: string;
  linked: boolean;
  implemented: boolean;
  tested: boolean;
  verified: boolean;
  stale: boolean;
  current_revision: number;
  links: MatrixLink[];
}

export interface OmittedRequirement {
  key: string;
  title: string;
  reason: string;
  chars: number;
}

export interface TaskContext {
  project_id: string;
  requirements: Requirement[];
  omitted: OmittedRequirement[];
  unknown: string[];
  budget_chars: number;
  used_chars: number;
}

/** One item from the sidecar's tolerant parse — whatever fields the human
 *  actually wrote down (`docs/requirements-format.md`); nothing here is
 *  defaulted, a missing field is just absent. `key` is a readable reference
 *  only in the sidecar, never authoritative — creating a real requirement
 *  from one of these always gets a fresh server-assigned `REQ-N`. */
export interface SidecarItem {
  key?: string;
  title?: string;
  text?: string;
  source?: string;
  status?: string;
  acceptance?: string[];
}

export interface SidecarResult {
  items: SidecarItem[];
  errors: string[];
  path: string;
}

export class RequirementsApiError extends ApiError {
  readonly errorClass: string | null;
  readonly payload: Record<string, unknown>;

  constructor(message: string, status: number, errorClass: string | null, payload: Record<string, unknown>) {
    super(message, status);
    this.name = 'RequirementsApiError';
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
    throw new RequirementsApiError(await responseReason(response, path), response.status, errorClass, payload);
  }
  return (await response.json()) as T;
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/requirements`;

function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
}

function patchJson<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
}

// ---------------------------------------------------------------------------
// Listing / retrieval
// ---------------------------------------------------------------------------

export function listRequirements(
  projectId: string,
  opts: { status?: string; source?: string; q?: string } = {},
  signal?: AbortSignal,
): Promise<{ requirements: Requirement[] }> {
  const params = new URLSearchParams();
  if (opts.status) params.set('status', opts.status);
  if (opts.source) params.set('source', opts.source);
  if (opts.q) params.set('q', opts.q);
  const qs = params.toString();
  return get(`${base(projectId)}${qs ? `?${qs}` : ''}`, signal);
}

export function getRequirement(projectId: string, key: string, signal?: AbortSignal): Promise<{ requirement: Requirement }> {
  return get(`${base(projectId)}/${encodeURIComponent(key)}`, signal);
}

export function getRevisions(projectId: string, key: string, signal?: AbortSignal): Promise<{ revisions: RequirementRevision[] }> {
  return get(`${base(projectId)}/${encodeURIComponent(key)}/revisions`, signal);
}

export function listLinks(projectId: string, key: string, signal?: AbortSignal): Promise<{ links: RequirementLink[] }> {
  return get(`${base(projectId)}/${encodeURIComponent(key)}/links`, signal);
}

export function getRequirementMatrix(projectId: string, key: string, signal?: AbortSignal): Promise<{ matrix: MatrixRow }> {
  return get(`${base(projectId)}/${encodeURIComponent(key)}/matrix`, signal);
}

export function getProjectMatrix(projectId: string, signal?: AbortSignal): Promise<{ matrix: MatrixRow[] }> {
  return get(`${base(projectId)}/matrix`, signal);
}

export function getSidecar(projectId: string, signal?: AbortSignal): Promise<SidecarResult> {
  return get(`${base(projectId)}/sidecar`, signal);
}

// ---------------------------------------------------------------------------
// Mutation
// ---------------------------------------------------------------------------

export interface RequirementCreateInput {
  title: string;
  text?: string;
  source?: RequirementSource;
  acceptance?: string[];
  proposed_by?: ProposedBy;
  status?: RequirementStatus;
}

export function createRequirement(projectId: string, input: RequirementCreateInput): Promise<{ requirement: Requirement }> {
  return post(base(projectId), input);
}

export interface RequirementUpdateInput {
  title?: string;
  text?: string;
  source?: RequirementSource;
  acceptance?: string[];
  status?: RequirementStatus;
  change_note?: string;
}

/** Every write goes through this one call. `by` is always `'human'` — see
 *  the module docstring; note that `by` follows the spread of `patchBody`
 *  below, so it always wins even if a caller somehow had one to pass. */
export function updateRequirement(
  projectId: string,
  key: string,
  patchBody: RequirementUpdateInput,
): Promise<{ requirement: Requirement }> {
  return patchJson(`${base(projectId)}/${encodeURIComponent(key)}`, { ...patchBody, by: 'human' });
}

/** `true` for the two statuses ADP-18 reserves for a human decision — used
 *  by the screen to gate the Accept/Reject controls on `proposed`, and to
 *  label a still-`proposed` model suggestion as awaiting one. */
export function humanOnlyStatus(status: RequirementStatus): boolean {
  return status === 'accepted' || status === 'rejected';
}

export function acceptRequirement(projectId: string, key: string, changeNote = ''): Promise<{ requirement: Requirement }> {
  return updateRequirement(projectId, key, { status: 'accepted', change_note: changeNote });
}

export function rejectRequirement(projectId: string, key: string, changeNote = ''): Promise<{ requirement: Requirement }> {
  return updateRequirement(projectId, key, { status: 'rejected', change_note: changeNote });
}

export interface LinkCreateInput {
  kind: LinkKind;
  target: string;
  revision?: string;
}

export function addLink(projectId: string, key: string, input: LinkCreateInput): Promise<{ link: RequirementLink }> {
  return post(`${base(projectId)}/${encodeURIComponent(key)}/links`, input);
}

/** Withdraw one link. Scoped by requirement on the server, so a link id from
 *  another project or requirement comes back as 404, never as a removal. */
export function removeLink(projectId: string, key: string, linkId: string): Promise<{ removed: boolean; link_id: string }> {
  return request(`${base(projectId)}/${encodeURIComponent(key)}/links/${encodeURIComponent(linkId)}`, { method: 'DELETE' });
}

export interface ContextForTaskInput {
  files?: string[];
  keys?: string[];
  budget_chars?: number;
}

export function contextForTask(projectId: string, input: ContextForTaskInput = {}): Promise<TaskContext> {
  return post(`${base(projectId)}/context`, input);
}

// ---------------------------------------------------------------------------
// Presentation helpers — pure, exercised by studio/checks/requirements.check.mjs
// without a DOM.
// ---------------------------------------------------------------------------

export type Tone = 'ok' | 'bad' | 'warn' | 'neutral';

export const STATUS_TONE: Record<RequirementStatus, Tone> = {
  proposed: 'neutral',
  accepted: 'ok',
  rejected: 'bad',
  superseded: 'neutral',
};

export const LINK_STATE_TONE: Record<LinkState, Tone> = {
  linked: 'ok',
  needs_review: 'warn',
  stale: 'warn',
  unknown: 'neutral',
};

/** "one criterion per line" <-> the API's plain string array, both
 *  directions — the textarea never sees an array, the server never sees
 *  blank lines. */
export function acceptanceFromLines(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

export function acceptanceToLines(acceptance: string[]): string {
  return (acceptance ?? []).join('\n');
}

/** Same shape for the "files"/"keys" boxes the context-for-task tool takes —
 *  one entry per line, blank lines dropped. */
export function linesToList(text: string): string[] {
  return acceptanceFromLines(text);
}
