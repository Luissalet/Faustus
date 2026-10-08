import { ApiError, asArray, getJson, responseReason } from './api';

/**
 * Harness refinement proposals (`src/harness_refinement`,
 * `routes/harness_proposals_routes.py`). After a task, the assistant may
 * propose the smallest edit to its own harness state — project instructions,
 * a skill, a memory entry, a subagent spec. Nothing applies by itself: the
 * person approves, and every applied edit can be undone by id.
 */

export type HarnessAxis = 'prompt_layer' | 'skill' | 'memory' | 'subagent_spec';
export type HarnessOp = 'create' | 'update' | 'delete';
export type HarnessStatus = 'pending' | 'applied' | 'rejected' | 'undone';

export interface HarnessEvidence {
  id?: string;
  session_id?: string;
  message_id?: string | number;
  turn?: number;
  kind?: string;
  quote?: string;
}

export interface HarnessProposal {
  id: string;
  axis: HarnessAxis;
  target: string;
  op: HarnessOp;
  diff: string;
  rationale: string;
  evidence: HarnessEvidence[];
  trigger: string;
  created_at: number;
  status: HarnessStatus;
  applied_at: number | null;
  undo_of: string | null;
  session_id: string;
  model: string;
  risk_flags: string[];
  stale?: boolean;
  has_delegate?: boolean;
  before?: string | null;
  after?: string | null;
}

export interface HarnessLogEntry {
  ts?: number;
  event: string;
  trigger?: string;
  result?: string;
  proposal_id?: string;
  session_id?: string;
}

export interface HarnessStatusInfo {
  enabled: boolean;
  counts: Record<string, number>;
  axes: HarnessAxis[];
  queued?: number;
}

export interface ProposeOutcome {
  status: 'proposed' | 'none' | 'skipped' | 'error';
  reason?: string;
  message?: string;
  proposal?: HarnessProposal;
}

async function send<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return (await response.json()) as T;
}

const BASE = '/api/harness/proposals';

export async function listHarnessProposals(status?: HarnessStatus): Promise<HarnessProposal[]> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : '';
  return asArray<HarnessProposal>(await getJson<unknown>(`${BASE}${qs}`), 'proposals');
}

export async function getHarnessProposal(id: string): Promise<{ proposal: HarnessProposal; log: HarnessLogEntry[] }> {
  const raw = await getJson<{ proposal: HarnessProposal; log?: HarnessLogEntry[] }>(`${BASE}/${encodeURIComponent(id)}`);
  return { proposal: raw.proposal, log: raw.log ?? [] };
}

export async function harnessStatus(): Promise<HarnessStatusInfo> {
  const raw = await getJson<Partial<HarnessStatusInfo>>(`${BASE}/status`);
  return { enabled: Boolean(raw.enabled), counts: raw.counts ?? {}, axes: raw.axes ?? [], queued: raw.queued };
}

export async function harnessLog(limit = 50): Promise<HarnessLogEntry[]> {
  return asArray<HarnessLogEntry>(await getJson<unknown>(`${BASE}/log?limit=${limit}`), 'log');
}

export async function proposeForSession(sessionId: string, force = false): Promise<ProposeOutcome> {
  return send<ProposeOutcome>(`${BASE}/propose`, { session_id: sessionId, force });
}

export async function approveHarnessProposal(id: string): Promise<HarnessProposal> {
  return (await send<{ proposal: HarnessProposal }>(`${BASE}/${encodeURIComponent(id)}/approve`)).proposal;
}

export async function rejectHarnessProposal(id: string, reason = ''): Promise<HarnessProposal> {
  return (await send<{ proposal: HarnessProposal }>(`${BASE}/${encodeURIComponent(id)}/reject`, { reason })).proposal;
}

export async function undoHarnessProposal(id: string): Promise<HarnessProposal> {
  return (await send<{ proposal: HarnessProposal }>(`${BASE}/${encodeURIComponent(id)}/undo`)).proposal;
}

/** Split a unified diff into lines tagged add / del / hunk / ctx for display. */
export function diffLines(diff: string): { kind: 'add' | 'del' | 'hunk' | 'ctx'; text: string }[] {
  return (diff || '')
    .split('\n')
    .filter((l, i, all) => !(i === all.length - 1 && l === ''))
    .map((text) => {
      if (text.startsWith('+++') || text.startsWith('---') || text.startsWith('@@')) return { kind: 'hunk' as const, text };
      if (text.startsWith('+')) return { kind: 'add' as const, text };
      if (text.startsWith('-')) return { kind: 'del' as const, text };
      return { kind: 'ctx' as const, text };
    });
}
