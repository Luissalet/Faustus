import { ApiError, asArray, getJson, responseReason } from './api';
import { saveSettings } from './settings';

/**
 * Harness refinement proposals (`src/harness_refinement`,
 * `routes/harness_proposals_routes.py`). After a task, the assistant proposes
 * the smallest edit to its own harness state — project instructions, a
 * skill, a memory entry, a subagent spec. With the probe on (the default) each
 * candidate is first tried on a repeat of the task and the one that measurably
 * helps is applied by itself; with auto-apply off it waits for the person.
 * Every applied edit can be undone by id.
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
  applied_by?: string;
  probe_id?: string;
  probe_line?: 'inv' | 'dom';
  probe_result?: 'adopted' | 'discarded';
  probe_reason?: string;
  probe_verdict?: string;
  probe_evidence?: ProbeCitation[];
}

export interface ProbeCitation {
  run: string;
  step: number;
  quote?: string;
}

export type ProbeStatus = 'running' | 'done' | 'skipped' | 'aborted' | 'error';

export interface ProbeCandidate {
  id: string;
  line: 'inv' | 'dom';
  axis?: HarnessAxis;
  target?: string;
  op?: HarnessOp;
  valid?: boolean;
  result?: string | null;
  reason?: string;
  proposal_id?: string | null;
  diff?: string;
  rationale?: string;
  accepted?: boolean;
  rejected_because?: string | null;
  won?: boolean;
}

export interface ProbeSummary {
  id: string;
  session_id?: string;
  status: ProbeStatus;
  phase?: string;
  created_at?: number;
  finished_at?: number | null;
  domain?: string;
  k?: number;
  task: string;
  candidates: ProbeCandidate[];
  winner?: string | null;
  mode?: 'auto' | 'pending' | 'none' | null;
  applied?: boolean | null;
  reason?: string;
  experience?: string | null;
}

export interface ProbeStep {
  n: number;
  tool?: string;
  args?: string;
  ok?: boolean;
  output?: string;
}

export interface ProbeRollout {
  id: string;
  arm: string;
  candidate?: string;
  title?: string;
  delivered: boolean;
  rounds: number;
  seconds: number;
  error?: string;
  text?: string;
  steps: ProbeStep[];
}

export interface ProbeVerdict {
  candidate: string;
  accepted: boolean;
  reason?: string;
  rejected_because?: string | null;
  evidence: ProbeCitation[];
}

export interface ProbeRecord extends Omit<ProbeSummary, 'task' | 'experience'> {
  task: { text: string; followup?: string; kind?: string } | null;
  rollouts: ProbeRollout[];
  compose: { notes?: string; keep_base?: boolean; verdicts: ProbeVerdict[] } | null;
  selection: { seed?: number; winner?: string; why?: string; pool?: { label: string; run: string }[] } | null;
  experience: { status?: string; reason?: string; path?: string; entry?: HarnessLesson } | null;
  error?: string;
}

export interface HarnessLesson {
  title?: string;
  cue: string;
  lesson: string;
  evidence: string;
  confidence: string;
  verdict: string;
}

export interface LessonDomain {
  key: string;
  name: string;
  kind: string;
  entries: number;
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
  probe: boolean;
  auto_apply: boolean;
  probe_k: number;
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
  return {
    enabled: Boolean(raw.enabled), counts: raw.counts ?? {}, axes: raw.axes ?? [], queued: raw.queued,
    probe: raw.probe ?? true, auto_apply: raw.auto_apply ?? true, probe_k: raw.probe_k ?? 2,
  };
}

export async function listProbes(): Promise<ProbeSummary[]> {
  return asArray<ProbeSummary>(await getJson<unknown>(`${BASE}/probes?limit=20`), 'probes');
}

export async function getProbe(id: string): Promise<{ probe: ProbeRecord; proposals: HarnessProposal[] }> {
  const raw = await getJson<{ probe: ProbeRecord; proposals?: HarnessProposal[] }>(`${BASE}/probes/${encodeURIComponent(id)}`);
  return { probe: raw.probe, proposals: raw.proposals ?? [] };
}

/** Starts a round in the background; the id is polled with `getProbe` / `listProbes`. */
export async function startProbe(sessionId: string, force = false): Promise<{ probe_id: string; status: string }> {
  return send<{ probe_id: string; status: string }>(`${BASE}/probes`, { session_id: sessionId, force });
}

export async function listLessonDomains(): Promise<LessonDomain[]> {
  return asArray<LessonDomain>(await getJson<unknown>(`${BASE}/lessons`), 'domains');
}

export async function getLessons(domain: string): Promise<HarnessLesson[]> {
  return asArray<HarnessLesson>(await getJson<unknown>(`${BASE}/lessons?project_id=${encodeURIComponent(domain)}`), 'entries');
}

export async function setHarnessMode(patch: { harness_refinement_enabled?: boolean; harness_refinement_probe?: boolean; harness_refinement_auto_apply?: boolean }): Promise<void> {
  await saveSettings(patch);
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
