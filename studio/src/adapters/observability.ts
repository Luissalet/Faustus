import { asArray, getJson } from './api';

/**
 * OBS-01: `GET /api/observability/trace/{call_id}` — everything ONE tool
 * call produced, joined by `call_id` alone (`src/agent_runs.py::trace_for_call`):
 * its events (already carrying `trace_id`/`step_id`), the artifact manifest
 * row(s) it made (if any) and its command_guard receipt (if any) — so a
 * person can follow "a tool ran → what did it write → was it allowed"
 * without grepping three logs by hand.
 */

export interface TraceEvent {
  type: string;
  tool: string;
  round: number | null;
  /** Everything else the raw SSE payload carried (command, output, exit_code, …) — shown verbatim, never reshaped, since the shape differs per event type. */
  raw: Record<string, unknown>;
}

export interface TraceArtifact {
  occurrenceId: string;
  manifestId: string;
  version: number | null;
  state: string;
  format: string;
  byteSize: number | null;
  sha256: string;
  generator: string;
  createdAt: string;
  label: string;
  kind: string;
  owner: string;
}

export interface CallTrace {
  callId: string;
  events: TraceEvent[];
  artifacts: TraceArtifact[];
  /** The command_guard decision record, verbatim — shape varies by guard version, so this is shown as-is, never reshaped. */
  receipt: Record<string, unknown> | null;
  /** True iff at least one of events/artifacts/receipt turned up something — tells "nothing ever happened under this id" apart from "the event is here but the rest legitimately isn't". */
  found: boolean;
}

function traceEventFrom(raw: Record<string, unknown>): TraceEvent {
  return {
    type: String(raw.type ?? ''),
    tool: String(raw.tool ?? ''),
    round: typeof raw.round === 'number' ? raw.round : null,
    raw,
  };
}

function traceArtifactFrom(raw: Record<string, unknown>): TraceArtifact {
  return {
    occurrenceId: String(raw.occurrence_id ?? ''),
    manifestId: String(raw.manifest_id ?? ''),
    version: typeof raw.version === 'number' ? raw.version : null,
    state: String(raw.state ?? ''),
    format: String(raw.format ?? ''),
    byteSize: typeof raw.byte_size === 'number' ? raw.byte_size : null,
    sha256: String(raw.sha256 ?? ''),
    generator: String(raw.generator ?? ''),
    createdAt: String(raw.created_at ?? ''),
    label: String(raw.label ?? ''),
    kind: String(raw.kind ?? ''),
    owner: String(raw.owner ?? ''),
  };
}

/** Exported for tests (studio/checks/l69b-obs01-trace.check.mjs): pure parsing, no fetch. */
export function callTraceFrom(raw: Record<string, unknown>): CallTrace {
  return {
    callId: String(raw.call_id ?? ''),
    events: asArray<Record<string, unknown>>(raw.events).map(traceEventFrom),
    artifacts: asArray<Record<string, unknown>>(raw.artifacts).map(traceArtifactFrom),
    receipt: raw.receipt && typeof raw.receipt === 'object' ? (raw.receipt as Record<string, unknown>) : null,
    found: Boolean(raw.found),
  };
}

/**
 * `sessionId` narrows the lookup to a session the caller owns (any signed-in
 * user); omitted, the search reaches every session on the instance and is
 * admin-only — the same scoping `routes/observability_routes.py` enforces.
 */
export async function traceForCall(callId: string, sessionId?: string, signal?: AbortSignal): Promise<CallTrace> {
  const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
  return callTraceFrom(await getJson<Record<string, unknown>>(`/api/observability/trace/${encodeURIComponent(callId)}${qs}`, signal));
}
