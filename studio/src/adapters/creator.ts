import { useEffect, useState } from 'react';
import { ApiError, responseReason } from './api';

/**
 * WP05 — Studio adapter over the Creator API (`routes/creator_*.py`):
 * documents/profile (WP02), library/lineage (WP03), capabilities/params
 * (WP07), preflight/approve (WP09) and physical resources (WP30).
 *
 * Every route below answers 404 with `{"detail": "creator is not enabled"}`
 * while the `creator_enabled` setting is off (CONTRATO.md rule 5) — this
 * adapter does not special-case that response, it is just an `ApiError`
 * with `status === 404`; the screen decides what a 404 on `/capabilities`
 * means (disabled) versus a 404 on `/documents/{id}` (not found/not yours).
 *
 * `apply_command` on a stale `expected_revision` answers 409 with
 * `{"reason": "revision_conflict", "current_revision": n}` — surfaced here
 * as `RevisionConflictError` so a screen can offer "reload" without parsing
 * strings (same pattern `AlternativesApiError` uses for its own 409/409-like
 * conflict, see `adapters/alternatives.ts`).
 */

export type DocumentKind = 'canvas' | 'timeline' | 'transcript' | 'song' | 'storyboard';
export type DocumentState = 'proposed' | 'accepted';

export interface CreatorDocument {
  id: string;
  project_id: string;
  kind: DocumentKind | string;
  schema_version: number;
  revision: number;
  state: DocumentState | string;
  asset_refs: string[];
  content: unknown;
  created_at: string;
  updated_at: string;
}

export interface CreatorCapabilities {
  creator_enabled: boolean;
  project_id: string;
  document_kinds: string[];
  document_states: string[];
  media_backends: unknown;
}

export interface ApplyCommandResult {
  document: CreatorDocument;
  applied: boolean;
  deduped: boolean;
}

export interface CommandHistoryEntry {
  command_id: string;
  applied: boolean;
  [key: string]: unknown;
}

export interface LibraryItemMedia {
  duration?: number | null;
  width?: number | null;
  height?: number | null;
  codec?: string | null;
  sample_rate?: number | null;
  [key: string]: unknown;
}

export interface LibraryItem {
  id: string;
  kind: string;
  label: string;
  sha256: string;
  byte_size: number;
  media_type: string | null;
  project_id: string;
  run_id: string;
  recipe: string;
  skill_id: string;
  partial: boolean;
  created_at: string;
  download_url: string;
  media: LibraryItemMedia | null;
}

export interface LineageEdge {
  [key: string]: unknown;
}

export interface DeploymentSummary {
  deployment_id: string;
  [key: string]: unknown;
}

export interface CapabilityProfile {
  deployment_id?: string;
  axes?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ParamSchema {
  engine: string;
  task: string;
  [key: string]: unknown;
}

export interface ParamValidationResult {
  ok: boolean;
  problems?: unknown[];
  [key: string]: unknown;
}

export interface PreflightRequest {
  project_id: string;
  operation: string;
  engine: string;
  deployment_id?: string;
  params?: Record<string, unknown>;
  inputs?: string[];
}

export interface PreflightReport {
  ok: boolean;
  missing: { field?: string; reason?: string; [key: string]: unknown }[];
  estimate: { tokens: number | null; seconds: number | null; vram_bytes: number | null; cost_usd: number | null; notes: string[] };
  admission: { fits: boolean; reason: string };
  requires_approval: boolean;
  approval_digest: string | null;
  approval_plan: Record<string, unknown> | null;
  budget: { reservation_needed: number | null; ceiling: number | null; remaining: number | null; verdict: string; reason: string };
  operation: string;
  engine: string;
  deployment_id: string;
  project_id: string;
  created_at: number;
}

export interface DeviceInventory {
  schema_version: number;
  gpus: unknown[];
  ram: unknown;
  disk: unknown;
  cpu: unknown;
  mode: string;
  vram_margin_bytes: number;
}

export interface ResourcesSnapshot {
  creator_enabled: boolean;
  inventory: DeviceInventory;
  active_admissions: { run_id: string; device: string; pool_id: string; footprint: unknown; acquired_at: number }[];
  queue: { run_id: string; workflow_id: string; workflow_version: string; engine_url: string; owner: string; reason: string; created_at: string }[];
}

export class CreatorApiError extends ApiError {
  readonly payload: Record<string, unknown>;

  constructor(message: string, status: number, payload: Record<string, unknown>) {
    super(message, status);
    this.name = 'CreatorApiError';
    this.payload = payload;
  }
}

/** 409 on `apply_command`: the caller's `expected_revision` is stale. Carries
 *  the document's real current revision so the screen can reload and retry
 *  instead of guessing or silently overwriting. */
export class RevisionConflictError extends CreatorApiError {
  readonly currentRevision: number;

  constructor(message: string, payload: Record<string, unknown>) {
    super(message, 409, payload);
    this.name = 'RevisionConflictError';
    const raw = payload.current_revision;
    this.currentRevision = typeof raw === 'number' ? raw : Number(raw) || 0;
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

/** Decodes the response, and turns a 409 revision conflict into a typed
 *  `RevisionConflictError` rather than a generic `ApiError` -- every other
 *  non-OK response (404 disabled/not-found, 400 validation) stays a plain
 *  `CreatorApiError` the screen reads `.status` and `.message` from. */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const payload = await payloadOf(response);
    const detail = payload.detail && typeof payload.detail === 'object'
      ? (payload.detail as Record<string, unknown>)
      : payload;
    const message = await responseReason(response, path);
    if (response.status === 409 && detail.reason === 'revision_conflict') {
      throw new RevisionConflictError(message, detail);
    }
    throw new CreatorApiError(message, response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
  });
}

function put<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
  });
}

const q = (params: Record<string, string | undefined>): string => {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) usp.set(k, v);
  const s = usp.toString();
  return s ? `?${s}` : '';
};

// ── capabilities (WP02 flag surface) ────────────────────────────────────

export function getCapabilities(projectId: string, signal?: AbortSignal): Promise<CreatorCapabilities> {
  return get(`/api/creator/capabilities${q({ project_id: projectId })}`, signal);
}

// ── documents (WP02) ────────────────────────────────────────────────────

export function listDocuments(projectId: string, signal?: AbortSignal): Promise<{ documents: CreatorDocument[] }> {
  return get(`/api/creator/documents${q({ project_id: projectId })}`, signal);
}

export function createDocument(
  projectId: string, kind: DocumentKind | string, content: unknown, state?: DocumentState | string,
): Promise<CreatorDocument> {
  return post('/api/creator/documents', { project_id: projectId, kind, content, state });
}

export function getDocument(docId: string, signal?: AbortSignal): Promise<CreatorDocument> {
  return get(`/api/creator/documents/${encodeURIComponent(docId)}`, signal);
}

/** Throws `RevisionConflictError` on a stale `expected_revision` (409). */
export function applyCommand(
  docId: string, commandId: string, expectedRevision: number, op: Record<string, unknown>,
): Promise<ApplyCommandResult> {
  return post(`/api/creator/documents/${encodeURIComponent(docId)}/commands`, {
    command_id: commandId, expected_revision: expectedRevision, op,
  });
}

export function getDocumentHistory(docId: string, signal?: AbortSignal): Promise<{ commands: CommandHistoryEntry[] }> {
  return get(`/api/creator/documents/${encodeURIComponent(docId)}/history`, signal);
}

// ── profile (WP02, on ProjectStore.context_items) ──────────────────────

export function getCreatorProfile(projectId: string, signal?: AbortSignal): Promise<{ project_id: string; profile: Record<string, unknown> }> {
  return get(`/api/creator/profile${q({ project_id: projectId })}`, signal);
}

export function putCreatorProfile(projectId: string, profile: Record<string, unknown>): Promise<{ project_id: string; profile: Record<string, unknown> }> {
  return put(`/api/creator/profile${q({ project_id: projectId })}`, profile);
}

// ── library + lineage (WP03) ────────────────────────────────────────────

export function listLibrary(
  projectId: string, filters: { kind?: string; tag?: string; q?: string } = {}, signal?: AbortSignal,
): Promise<{ project_id: string; items: LibraryItem[] }> {
  return get(`/api/creator/library${q({ project_id: projectId, kind: filters.kind, tag: filters.tag, q: filters.q })}`, signal);
}

export function exportManifest(projectId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
  return get(`/api/creator/library/export-manifest${q({ project_id: projectId })}`, signal);
}

export function getLineage(
  occurrenceId: string, signal?: AbortSignal,
): Promise<{ occurrence_id: string; ancestors: LineageEdge[]; descendants: LineageEdge[] }> {
  return get(`/api/creator/library/${encodeURIComponent(occurrenceId)}/lineage`, signal);
}

export function buildProxy(occurrenceId: string): Promise<Record<string, unknown>> {
  return post(`/api/creator/library/${encodeURIComponent(occurrenceId)}/proxy`);
}

// ── capabilities/params (WP07) ──────────────────────────────────────────

export function listKnownModels(signal?: AbortSignal): Promise<{ deployments: DeploymentSummary[] }> {
  return get('/api/creator/capabilities/models', signal);
}

export function getCapabilityProfile(deploymentId: string, signal?: AbortSignal): Promise<CapabilityProfile> {
  return get(`/api/creator/capabilities/${encodeURIComponent(deploymentId)}`, signal);
}

export function getParamSchema(engine: string, task: string, signal?: AbortSignal): Promise<{ schema: ParamSchema }> {
  return get(`/api/creator/params${q({ engine, task })}`, signal);
}

export function validateParams(engine: string, task: string, params: Record<string, unknown>): Promise<ParamValidationResult> {
  return post('/api/creator/params/validate', { engine, task, params });
}

// ── preflight + approve (WP09) ──────────────────────────────────────────

export function runPreflight(body: PreflightRequest): Promise<PreflightReport> {
  return post('/api/creator/preflight', body);
}

/** The one endpoint that actually opens+grants an approval for a Creator
 *  operation; gated server-side on `require_human` (a tool-call token
 *  cannot reach it). A 409 here means the plan changed since the digest was
 *  computed -- re-run preflight and approve the new digest. */
export function approvePreflight(digest: string, body: PreflightRequest): Promise<{ approval: unknown; digest: string }> {
  return post(`/api/creator/preflight/${encodeURIComponent(digest)}/approve`, body);
}

// ── resources (WP30) ────────────────────────────────────────────────────

export function getResources(signal?: AbortSignal): Promise<ResourcesSnapshot> {
  return get('/api/creator/resources', signal);
}

// ── nav visibility (WP05) ───────────────────────────────────────────────

/**
 * Whether Creator's sidebar entry should show at all: `null` while the one
 * check is in flight, then the real flag. A 404 on `/capabilities` means
 * `creator_enabled` is off (CONTRATO.md rule 5) -- the tool disappears from
 * the rail, exactly like any other feature-flagged destination; opening
 * `/creator` directly still works and explains itself (the screen redoes
 * this same check with a real `project_id`, since this probe intentionally
 * sends none).
 */
export function useCreatorAvailable(): boolean | null {
  const [available, setAvailable] = useState<boolean | null>(null);
  useEffect(() => {
    let cancelled = false;
    getCapabilities('')
      .then(() => { if (!cancelled) setAvailable(true); })
      .catch(() => { if (!cancelled) setAvailable(false); });
    return () => { cancelled = true; };
  }, []);
  return available;
}
