import { t } from '../i18n';
import { ApiError, CLIENT_API_VERSION, CLIENT_VERSION_HEADER } from './api';

/**
 * Delivery packages (`/api/delivery-packages`, Hangar E33), shaped for the
 * "Delivery" screen.
 *
 * Product rule kept here as a pure function: a package is drawn as ready only
 * when the server said `ready` AND the zip reopened identically
 * (`reimport.ok`). A file the readers could not open, or one with no reader,
 * is never drawn in the verified tone.
 */

export type Role = 'editable' | 'export' | 'data' | 'instructions' | 'example' | 'other';
export const ROLES: Role[] = ['editable', 'export', 'data', 'instructions', 'example', 'other'];
export type Tone = 'good' | 'warn' | 'bad' | 'unknown';

export interface Verification { status: string; reader: string | null; level?: string; reason?: string; facts?: Record<string, unknown> }
export interface PackageItem { role: Role; name: string; source_path: string; package_path: string; sha256: string; size: number; verification: Verification }
export interface ReimportRow { package_path: string; status: string; same_hash?: boolean; reason?: string }
export interface DeliveryPackage {
  id: string; name: string; created: string; ready: boolean; items: PackageItem[];
  excluded: { path: string; reason: string }[]; instructions: string; limitations: string[]; gaps: string[];
  software: { root: string; verified: boolean; counts: Record<string, number> | null } | null;
  reimport?: { ok: boolean; items: ReimportRow[] }; zip?: { path: string; sha256: string; size: number; copied_to?: string };
}
export interface PackageRow { id: string; name: string; created: string; ready: boolean; items: number; checks: Record<string, number>; gaps: number; excluded: number; zip_size?: number }
export interface DraftItem { role: Role; path: string; authorized: boolean }

export function statusTone(status: string): Tone {
  if (status === 'verified') return 'good';
  if (status === 'failed' || status === 'hash_mismatch' || status === 'missing') return 'bad';
  if (status === 'not_checked') return 'warn';
  return 'unknown';
}

export function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    verified: t('Opens'), failed: t('Does not open'), not_checked: t('No reader'),
    hash_mismatch: t('Changed in the zip'), missing: t('Missing from the zip'),
  };
  return labels[status] ?? status;
}

export function roleLabel(role: string): string {
  const labels: Record<string, string> = {
    editable: t('Editable'), export: t('Export'), data: t('Data source'), instructions: t('Instructions'),
    example: t('Example'), other: t('Other'),
  };
  return labels[role] ?? role;
}

/** Ready is earned twice: by the source checks and by reopening the zip. */
export function isReady(p: Pick<DeliveryPackage, 'ready' | 'reimport'>): boolean {
  return !!p.ready && !!p.reimport?.ok;
}

export function lines(text: string): string[] {
  return text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION,
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
    },
  });
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    /* not JSON */
  }
  if (!response.ok) {
    const o = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
    const reason = String(o.error || o.detail || `${path} ${t('responded')} ${response.status}`);
    throw new ApiError(reason, response.status);
  }
  return body as T;
}

const BASE = '/api/delivery-packages';

export async function listPackages(): Promise<PackageRow[]> {
  const r = await request<{ packages?: PackageRow[] }>(BASE);
  return Array.isArray(r.packages) ? r.packages : [];
}

export const loadPackage = (id: string) => request<DeliveryPackage>(`${BASE}/${encodeURIComponent(id)}`);

export function buildPackage(body: {
  name: string; items: DraftItem[]; instructions: string; limitations: string[]; software_root?: string; lang?: string;
}): Promise<DeliveryPackage> {
  const items = body.items.filter((i) => i.path.trim()).map((i) => ({ path: i.path.trim().replace(/^"|"$/g, ''), role: i.role, authorized: i.authorized }));
  return request<DeliveryPackage>(BASE, { method: 'POST', body: JSON.stringify({ ...body, items }) });
}

export const reverifyPackage = (id: string) =>
  request<{ reverify: { ok: boolean; zip_same_hash: boolean; items: ReimportRow[] } }>(`${BASE}/${encodeURIComponent(id)}/reverify`, { method: 'POST' });

export const downloadHref = (id: string) => `${BASE}/${encodeURIComponent(id)}/download`;
export const documentHref = (id: string) => `${BASE}/${encodeURIComponent(id)}/document`;

/* -- scope comparator (`/api/scope-comparisons`, Hangar E30) --------------- */

export type ScopeStatus = 'included' | 'changed' | 'added' | 'removed' | 'ambiguous';
export interface ScopeEffect {
  class: 'local' | 'partial' | 'pipeline' | 'none' | 'unknown'; recalculate?: string[]; deliverables?: string[];
  effort_hours?: [number, number]; no_effort_for?: string[]; reason?: string; items?: string[];
}
export interface ScopeRow {
  n: number; clause: string; status: ScopeStatus; agreed?: string; agreed_ref?: string; reason?: string;
  diff?: { attributes?: { from: string[]; to: string[] }; quantities?: Record<string, { from: string; to: string }> };
  effect?: ScopeEffect; options: string[]; question?: string;
}
export interface ScopeComparison {
  id: string; title: string; created: string; graph: string | null; graph_found: boolean | null; rows: ScopeRow[];
  counts: Record<ScopeStatus, number>; questions: string[]; negotiated: { clause: string; status: string }[]; limits: string;
}
export interface ScopeListRow { id: string; title: string; created: string; counts: Record<ScopeStatus, number>; questions: number; graph: string | null }

export function scopeTone(status: string): Tone {
  if (status === 'included') return 'good';
  if (status === 'ambiguous') return 'warn';
  if (status === 'added' || status === 'changed' || status === 'removed') return 'bad';
  return 'unknown';
}

export function scopeLabel(status: string): string {
  const labels: Record<string, string> = {
    included: t('Already included'), changed: t('Changed'), added: t('New work'), removed: t('Removed'), ambiguous: t('Ambiguous'),
  };
  return labels[status] ?? status;
}

export function effectLabel(e?: ScopeEffect): string {
  if (!e) return '';
  const labels: Record<string, string> = {
    local: t('Local change'), partial: t('Partial rework'), pipeline: t('Reruns the pipeline'), none: t('Nothing depends on it'),
    unknown: t('Effect not grounded'),
  };
  return labels[e.class] ?? e.class;
}

export async function listComparisons(): Promise<ScopeListRow[]> {
  const r = await request<{ comparisons?: ScopeListRow[] }>('/api/scope-comparisons');
  return Array.isArray(r.comparisons) ? r.comparisons : [];
}

export const loadComparison = (id: string) => request<ScopeComparison>(`/api/scope-comparisons/${encodeURIComponent(id)}`);

export function compareScope(body: {
  title: string; initial: string; agreement: string; new_request: string; graph: string; mode: string; effort_hours?: Record<string, [number, number]>;
}): Promise<ScopeComparison> {
  return request<ScopeComparison>('/api/scope-comparisons', { method: 'POST', body: JSON.stringify(body) });
}

/** "export_pdf: 1-2" per line -> {export_pdf: [1, 2]}; lines that do not parse are ignored. */
export function parseEffort(text: string): Record<string, [number, number]> {
  const out: Record<string, [number, number]> = {};
  for (const line of lines(text)) {
    const m = /^([A-Za-z0-9_.:-]+)\s*[:=]\s*(\d+(?:[.,]\d+)?)\s*(?:-|–|a|to)\s*(\d+(?:[.,]\d+)?)/.exec(line);
    if (m) out[m[1]] = [Number(m[2].replace(',', '.')), Number(m[3].replace(',', '.'))];
  }
  return out;
}
