/**
 * Adapter for the run records (`routes/run_report_routes.py`): where one turn's
 * time and tokens went, the messages sent to a live run and what became of
 * them, and workers whose parent run is gone.
 *
 * The rule the server keeps and this file keeps with it: a value nobody
 * reported is `null`, never `0`. The UI shows "unknown" for it.
 */
import { ApiError, asArray, getJson, responseReason } from './api';

export type CostPhaseKey =
  | 'main_rounds'
  | 'tools'
  | 'compaction'
  | 'recovery'
  | 'advisor'
  | 'workers'
  | 'retries'
  | 'other_model';

export type CostState = 'known' | 'partial' | 'unknown';

export interface CostItem {
  label: string;
  calls: number | null;
  durationMs: number | null;
  failed: number;
  unknownEffect: number;
}

export interface CostPhase {
  key: CostPhaseKey;
  calls: number;
  /** Sum of the calls that reported a duration; `null` when none did. */
  durationMs: number | null;
  durationUnknownCalls: number;
  inputTokens: number | null;
  inputTokensUnknownCalls: number;
  outputTokens: number | null;
  outputTokensUnknownCalls: number;
  estimatedCalls: number;
  costUsd: number | null;
  costState: CostState;
  errors: number;
  models: string[];
  items: CostItem[];
}

export interface TurnCost {
  found: boolean;
  reason: string;
  sessionId: string;
  runId: string;
  state: string;
  totalMs: number | null;
  accountedMs: number | null;
  unaccountedMs: number | null;
  phases: CostPhase[];
  notes: string[];
}

function numOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function strOf(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function itemFrom(raw: Record<string, unknown>): CostItem {
  return {
    label: strOf(raw.tool) || strOf(raw.name) || strOf(raw.worker_id),
    calls: numOrNull(raw.calls) ?? (raw.tool_calls == null ? null : numOrNull(raw.tool_calls)),
    durationMs: numOrNull(raw.duration_ms),
    failed: numOrNull(raw.failed) ?? 0,
    unknownEffect: numOrNull(raw.unknown_effect) ?? 0,
  };
}

function phaseFrom(raw: Record<string, unknown>): CostPhase {
  const state = raw.cost_state === 'known' || raw.cost_state === 'partial' ? raw.cost_state : 'unknown';
  return {
    key: strOf(raw.key) as CostPhaseKey,
    calls: numOrNull(raw.calls) ?? 0,
    durationMs: numOrNull(raw.duration_ms),
    durationUnknownCalls: numOrNull(raw.duration_unknown_calls) ?? 0,
    inputTokens: numOrNull(raw.input_tokens),
    inputTokensUnknownCalls: numOrNull(raw.input_tokens_unknown_calls) ?? 0,
    outputTokens: numOrNull(raw.output_tokens),
    outputTokensUnknownCalls: numOrNull(raw.output_tokens_unknown_calls) ?? 0,
    estimatedCalls: numOrNull(raw.estimated_calls) ?? 0,
    costUsd: numOrNull(raw.cost_usd),
    costState: state,
    errors: numOrNull(raw.errors) ?? 0,
    models: asArray<unknown>(raw.models).map(String),
    items: asArray<Record<string, unknown>>(raw.items).map(itemFrom),
  };
}

export function turnCostFrom(raw: unknown): TurnCost {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  return {
    found: Boolean(r.found),
    reason: strOf(r.reason),
    sessionId: strOf(r.session_id),
    runId: strOf(r.run_id),
    state: strOf(r.state),
    totalMs: numOrNull(r.total_ms),
    accountedMs: numOrNull(r.accounted_ms),
    unaccountedMs: numOrNull(r.unaccounted_ms),
    phases: asArray<Record<string, unknown>>(r.phases).map(phaseFrom),
    notes: asArray<unknown>(r.notes).map(String).filter(Boolean),
  };
}

/** Where this turn's time and tokens went. `runId` names the turn (the id a
 *  reply carries as `metrics.runId`); without it the session's latest turn. */
export async function fetchTurnCost(sessionId: string, runId?: string | null, signal?: AbortSignal): Promise<TurnCost> {
  const query = runId ? `?run_id=${encodeURIComponent(runId)}` : '';
  return turnCostFrom(await getJson<unknown>(`/api/runs/${encodeURIComponent(sessionId)}/turn-cost${query}`, signal));
}

/* ── Messages sent to a live run (H19) ── */

export type SteerState = 'queued' | 'drained' | 'handed_off' | 'applied' | 'dropped' | 'redelivered';

export interface SteerReceipt {
  receiptId: string;
  state: SteerState;
  mode: 'steer' | 'send_after';
  reason: string;
  text: string;
}

export function steerReceiptFrom(raw: unknown): SteerReceipt | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const id = strOf(r.receipt_id);
  if (!id) return null;
  return {
    receiptId: id,
    state: (strOf(r.state) || 'queued') as SteerState,
    mode: r.mode === 'send_after' ? 'send_after' : 'steer',
    reason: strOf(r.reason),
    text: strOf(r.text),
  };
}

export interface ClaimedSend {
  receiptId: string;
  text: string;
}

/** "Send after" messages whose turn has ended. The server marks them handed
 *  off; the client sends each as a new turn and then acknowledges it. `blocked`
 *  says why none were handed over (a turn still running, an approval pending). */
export async function claimQueuedSends(sessionId: string): Promise<{ messages: ClaimedSend[]; blocked: string }> {
  try {
    const response = await fetch(`/api/chat/steer/${encodeURIComponent(sessionId)}/claim`, {
      method: 'POST',
      credentials: 'same-origin',
    });
    if (!response.ok) return { messages: [], blocked: String(response.status) };
    const body = (await response.json()) as { messages?: unknown; blocked?: unknown };
    const messages = asArray<Record<string, unknown>>(body.messages)
      .map((m) => ({ receiptId: strOf(m.receipt_id), text: strOf(m.text) }))
      .filter((m) => m.receiptId && m.text);
    return { messages, blocked: strOf(body.blocked) };
  } catch {
    return { messages: [], blocked: 'network' };
  }
}

/** Tell the server the message was sent as a turn. Only then is it `applied`. */
export async function acknowledgeQueuedSend(receiptId: string): Promise<boolean> {
  try {
    const response = await fetch(`/api/chat/steer/receipt/${encodeURIComponent(receiptId)}/ack`, {
      method: 'POST',
      credentials: 'same-origin',
    });
    return response.ok;
  } catch {
    return false;
  }
}

export async function fetchSteerReceipts(sessionId: string, signal?: AbortSignal): Promise<SteerReceipt[]> {
  const body = await getJson<{ receipts?: unknown }>(`/api/chat/steer/${encodeURIComponent(sessionId)}/receipts`, signal);
  return asArray<unknown>(body.receipts).map(steerReceiptFrom).filter((r): r is SteerReceipt => r !== null);
}

/* ── Workers whose parent run is gone (H19) ── */

export interface OrphanWorker {
  sessionId: string;
  workerId: string;
  kind: 'delegated_worker' | 'dispatch_job';
  name: string;
  parentState: string;
  reason: string;
  ageSeconds: number;
  toolCalls: number;
}

export async function fetchOrphans(signal?: AbortSignal): Promise<OrphanWorker[]> {
  const body = await getJson<{ orphans?: unknown }>('/api/agent/orphans', signal);
  return asArray<Record<string, unknown>>(body.orphans).map((o) => ({
    sessionId: strOf(o.session_id),
    workerId: strOf(o.worker_id),
    kind: o.kind === 'dispatch_job' ? 'dispatch_job' : 'delegated_worker',
    name: strOf(o.name) || strOf(o.role),
    parentState: strOf(o.parent_state),
    reason: strOf(o.reason),
    ageSeconds: numOrNull(o.age_s) ?? 0,
    toolCalls: numOrNull(o.tool_calls) ?? 0,
  }));
}

export async function killOrphan(key: string): Promise<boolean> {
  const path = `/api/agent/orphans/${encodeURIComponent(key)}/kill`;
  const response = await fetch(path, { method: 'POST', credentials: 'same-origin' });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  const body = (await response.json()) as { stopped?: unknown };
  return Boolean(body.stopped);
}
