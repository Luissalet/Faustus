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

/* -- commission office (`/api/commissions`, Hangar E29) ------------------- */

export const STAGES = ['opportunity', 'proposal', 'agreed', 'in_progress', 'delivered', 'closed', 'lost'] as const;
export interface ScopeVersion { version: number; kind: 'initial' | 'agreement' | 'change'; text: string; at: string;
  comparison?: { id: string; against: number; counts: Record<ScopeStatus, number>; questions: string[] } }
export interface Milestone { id: string; title: string; due: string; done: string | null; amount: number | null;
  deliverables: { title: string; package_id: string | null }[] }
export interface CommissionStatus {
  stage: string; agreement_version: number | null; pending_change: boolean; open_questions: string[];
  next_milestone: { id: string; title: string; due: string } | null; overdue: string[]; milestones_done: number;
  milestones_total: number; deliverables: { milestone: string; title: string; package_id: string | null; ready: boolean | null }[];
  deliverables_without_ready_package: string[]; billing_expected: number | null;
  payments_received?: number | null; payments_to_confirm?: string[]; people_linked?: number;
  meeting_items_pending?: number; meeting_open_questions?: string[];
  payments_partly_paid?: { label: string; received: number; amount: number }[]; reviews?: CommissionReview[];
}
export interface Commission {
  id: string; title: string; client: string; stage: string; summary: string; created: string; updated: string;
  budget: { amount: number; currency: string; note: string } | null; scope: ScopeVersion[]; milestones: Milestone[];
  contacts: { name: string; role: string; email: string; people_id: string }[]; files: { path: string; role: string; note: string }[];
  decisions: { date: string; text: string; by: string }[];
  payments: { label: string; amount: number | null; due: string; ledger_ref: string; confirmed?: boolean; received_on?: string;
    received?: number; receipts?: { tx_id: string; amount: number | null; date: string | null }[];
    ledger_check?: { at: string; error?: string; entries_scanned?: number; owed?: number; candidates?: LedgerCandidate[] } }[];
  events: { at: string; kind: string; text: string }[]; status: CommissionStatus;
  proposals?: { name: string; date: string; counts: Record<string, number>; masked: number }[];
  meetings?: OfficeMeeting[];
  proposal?: Proposal;
}
export interface Proposal {
  name: string; title: string; date: string; sections: { title: string; items: { text: string; origin: 'verified' | 'assumption' | 'question' }[] }[];
  leaks: { kind: string; value: string }[]; counts: Record<string, number>;
}
export const proposalHref = (id: string, name: string, kind: 'docx' | 'md') =>
  `/api/commissions/${encodeURIComponent(id)}/proposals/${encodeURIComponent(name)}.${kind}`;
export function originLabel(o: string): string {
  const labels: Record<string, string> = { verified: t('Verified'), assumption: t('Assumption'), question: t('Question') };
  return labels[o] ?? o;
}
export type MeetingKind = 'decision' | 'promise' | 'requirement' | 'proposal' | 'question';
export interface MeetingItem { n: number; kind: MeetingKind; text: string; speaker: string; time: string; line: number; firm: boolean;
  promoted: { as: string; at: string; ref: string; forced: boolean } | null }
export interface OfficeMeeting { id: string; source: string; meeting_id: string; date: string; at: string; items: MeetingItem[]; counts: Record<MeetingKind, number> }
export const MEETING_KINDS: MeetingKind[] = ['decision', 'promise', 'requirement', 'proposal', 'question'];
export function meetingKindLabel(k: string): string {
  const labels: Record<string, string> = { decision: t('Decision'), promise: t('Promise'), requirement: t('Requirement'), proposal: t('Proposal (tentative)'), question: t('Question') };
  return labels[k] ?? k;
}
export function meetingKindTone(k: string): Tone {
  return k === 'decision' || k === 'promise' ? 'good' : k === 'requirement' || k === 'question' ? 'warn' : 'unknown';
}
export async function recentMeetings(): Promise<{ id: string; title: string; date: string }[]> {
  const r = await request<{ meetings?: { id: string; title: string; date: string }[] }>('/api/commissions/meetings');
  return Array.isArray(r.meetings) ? r.meetings : [];
}
export interface LedgerCandidate { tx_id: string; date: string; amount: number; counterparty: string; score: number;
  kind?: 'exact' | 'combined' | 'partial'; tx_ids?: string[]; amounts?: number[]; dates?: string[] }
export interface CommissionReview { id: string; title: string; milestone: string; latest: number | null; delivered: number | null; open: number; versions: number }
export interface PersonCandidate { id: string; name: string; score: number | null; relation: string }
export async function peopleLookup(name: string): Promise<{ people: PersonCandidate[]; error?: string }> {
  return request(`/api/commissions/people-lookup?name=${encodeURIComponent(name)}`);
}
export interface CommissionRow { id: string; title: string; client: string; stage: string; updated: string; next_due: string | null; overdue: number; scope_versions: number }

export function stageLabel(stage: string): string {
  const labels: Record<string, string> = {
    opportunity: t('Opportunity'), proposal: t('Proposal'), agreed: t('Agreed'), in_progress: t('In progress'),
    delivered: t('Delivered'), closed: t('Closed'), lost: t('Lost'),
  };
  return labels[stage] ?? stage;
}

export async function listCommissions(): Promise<CommissionRow[]> {
  const r = await request<{ commissions?: CommissionRow[] }>('/api/commissions');
  return Array.isArray(r.commissions) ? r.commissions : [];
}
export const loadCommission = (id: string) => request<Commission>(`/api/commissions/${encodeURIComponent(id)}`);
export const createCommission = (body: { title: string; client: string; request: string }) =>
  request<Commission>('/api/commissions', { method: 'POST', body: JSON.stringify(body) });
export const commissionAction = (id: string, action: string, body: Record<string, unknown>) =>
  request<Commission>(`/api/commissions/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: JSON.stringify(body) });

// Review desk (Hangar E32): versions as snapshots, anchored comments, one delivered version.
export interface DeskAnchor { kind: string; label: string }
export interface DeskVersion { n: number; name: string; source: string; snapshot: string | null; sha256: string; size: number; at: string; note: string;
  reopen?: { status: string; reader?: string | null; facts?: Record<string, unknown>; reason?: string } }
export interface DeskComment { id: string; version: number; version_from: string; text: string; author: string; anchor: DeskAnchor | null; ref: string;
  at: string; status: string; about_older: boolean; fixed_in?: number; history: { at: string; from: string; to: string; note: string }[] }
export interface DeskStatus { latest: number | null; versions: number; counts: Record<string, number>; open_on_latest: string[]; open_from_older: string[];
  about_older_when_made: string[]; deliverable: string | null;
  delivered: { version: number; name: string; sha256: string; at: string; open_comments: string[] } | null;
  delivered_check: { snapshot_matches: boolean | null; source_still_matches: boolean | null; newer_versions: number[] } | null }
export interface ReviewDesk { id: string; title: string; created: string; updated: string; commission: string; milestone: string;
  versions: DeskVersion[]; comments: DeskComment[]; events: { at: string; kind: string; text: string }[]; status: DeskStatus }
export interface DeskRow { id: string; title: string; updated: string; versions: number; open: number; delivered: number | null; commission: string;
  milestone?: string; latest?: number | null }
// A version opened at an anchor (Hangar E22): `/api/review-desks/{id}/view`.
export interface AnchorView { version: number; name: string; ref: string; latest: number; from_snapshot: boolean; kind: string | null; found: boolean;
  reason?: string; anchor: DeskAnchor | null; page?: number; pages?: number; rect?: number[] | null; text?: string[]; text_in_rect?: string; image?: string;
  start?: number; end?: number | null; media?: 'video' | 'audio' | null; fragment?: string; sheet?: string; sheets?: string[]; cell?: string; value?: string | null;
  columns?: string[]; grid?: { row: number; cells: { ref: string; value: string | null; hit: boolean }[] }[];
  lines?: { n: number; text: string; hit: boolean }[]; declared_at?: number; object?: string; objects?: string[]; ext?: string; to?: string; cells_in_range?: number }
export async function viewDesk(id: string, q: { version?: number; anchor?: string; comment?: string; ref?: string }): Promise<AnchorView> {
  const p = new URLSearchParams({ image: 'true' });
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== '') p.set(k, String(v));
  const r = await request<{ view: AnchorView }>(`/api/review-desks/${encodeURIComponent(id)}/view?${p.toString()}`);
  return r.view;
}
export interface DeskComparison { a: number; b: number; same_content: boolean; size_delta: number; facts: Record<string, [unknown, unknown]>;
  text: { added: number; removed: number; diff: string[]; truncated: boolean } | null; comments_on_a: { id: string; status: string; anchor: string | null; text: string }[] }
export const COMMENT_STATES = ['open', 'in_progress', 'fixed', 'wontfix', 'answered'];
export function commentStateLabel(s: string): string {
  const labels: Record<string, string> = { open: t('Still open'), in_progress: t('In progress'), fixed: t('Fixed'), wontfix: t("Won't fix"), answered: t('Answered') };
  return labels[s] ?? s;
}
export function versionFromLabel(s: string): string {
  const labels: Record<string, string> = { given: t('chosen'), latest: t('latest when made'), 'named in the text': t('named in the text'), 'named in the feedback': t('named in the feedback') };
  return labels[s] ?? s;
}
export async function listDesks(): Promise<DeskRow[]> {
  const r = await request<{ desks?: DeskRow[] }>('/api/review-desks');
  return Array.isArray(r.desks) ? r.desks : [];
}
export const loadDesk = (id: string) => request<ReviewDesk>(`/api/review-desks/${encodeURIComponent(id)}`);
export const createDesk = (body: { title: string; path: string }) =>
  request<ReviewDesk>('/api/review-desks', { method: 'POST', body: JSON.stringify(body) });
export const deskAction = (id: string, action: string, body: Record<string, unknown>) =>
  request<ReviewDesk>(`/api/review-desks/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: JSON.stringify(body) });
export async function compareDesk(id: string, a: string, b: string): Promise<DeskComparison> {
  const r = await request<{ comparison: DeskComparison }>(`/api/review-desks/${encodeURIComponent(id)}/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
  return r.comparison;
}
export const deskVersionHref = (id: string, n: number) => `/api/review-desks/${encodeURIComponent(id)}/versions/${n}`;

/* -- delete (Faustus' own records only; source files are never touched) -- */

const del = (path: string) => request<Record<string, unknown>>(path, { method: 'DELETE' });
export const deletePackage = (id: string) => del(`/api/delivery-packages/${encodeURIComponent(id)}`);
export const deleteComparison = (id: string) => del(`/api/scope-comparisons/${encodeURIComponent(id)}`);
export const deleteDesk = (id: string) => del(`/api/review-desks/${encodeURIComponent(id)}`);
export const deleteCommission = (id: string, withLinked: boolean) =>
  del(`/api/commissions/${encodeURIComponent(id)}${withLinked ? '?with_linked=true' : ''}`);
export const deleteMeeting = (id: string) => del(`/api/commissions/meetings/${encodeURIComponent(id)}`);
