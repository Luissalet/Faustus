import { ApiError, getJson, responseReason } from './api';

/**
 * Capability probes for OpenAI-compatible model endpoints
 * (routes/model_probe_routes.py, src/openai_probes.py).
 *
 * Three facts kept apart: what the endpoint DECLARES (an operator setting), what
 * a probe OBSERVED for exactly this endpoint, connection revision and protocol,
 * and whether the server said it does NOT do it. `declared` and `probed` are
 * true / false / null (no statement, not observed or inconclusive).
 */

export type ProbeVerdict =
  | 'verified'
  | 'unsupported'
  | 'declared_unverified'
  | 'declared_unsupported'
  | 'undetermined'
  | 'not_declared';

export interface CapabilityRow {
  capability: string;
  declared: boolean | null;
  probed: boolean | null;
  verdict: ProbeVerdict;
  conflict: boolean;
  tested_at: string;
  status: number | null;
  reason: string;
}

export interface ProbeReport {
  endpoint_id: string;
  model: string;
  protocol: string;
  endpoint_revision: string;
  saved: boolean;
  capabilities: CapabilityRow[];
}

const API = '/api/model-probes/openai';

export function loadProbes(endpointId: string, model: string, signal?: AbortSignal): Promise<ProbeReport> {
  return getJson<ProbeReport>(`${API}?endpoint_id=${encodeURIComponent(endpointId)}&model=${encodeURIComponent(model)}`, signal);
}

/** Sends short requests to the endpoint (admin) and files what it did. */
export async function runProbes(endpointId: string, model: string, probes?: string[]): Promise<ProbeReport> {
  const path = API;
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ endpoint_id: endpointId, model, ...(probes ? { probes } : {}) }),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return (await response.json()) as ProbeReport;
}
