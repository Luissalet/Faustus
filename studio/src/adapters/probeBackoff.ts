import { ApiError, getJson, responseReason } from './api';

/**
 * Paused capability probes (`/api/model-endpoints/probe-backoff`, backend
 * `src/probe_backoff.py`): a model server that refused two connections in a
 * row is skipped by auto-detection (vision helper, context window, model
 * capabilities, hedge targets) for a window that doubles up to 15 minutes.
 */

export interface ProbeBackoffServer {
  server: string;
  failures: number;
  skipped: boolean;
  retry_in_s: number;
  until: number | null;
  last_error: string;
  last_at: number | null;
}

export interface ProbeBackoffStatus {
  servers: ProbeBackoffServer[];
  /** endpoint id -> server, for the endpoints that point at a listed server. */
  endpoints: Record<string, string>;
  /** endpoint id -> its name (optional: an older server omits it). */
  endpoint_names?: Record<string, string>;
  threshold: number;
  base_s: number;
  max_s: number;
}

export function getProbeBackoff(signal?: AbortSignal): Promise<ProbeBackoffStatus> {
  return getJson<ProbeBackoffStatus>('/api/model-endpoints/probe-backoff', signal);
}

/** Probe again now: one server (`url` or `endpoint_id`) or, with nothing, all of them. */
export async function clearProbeBackoff(target: { url?: string; endpoint_id?: string } = {}): Promise<number> {
  const path = '/api/model-endpoints/probe-backoff/clear';
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(target),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return Number(((await response.json()) as { cleared?: number }).cleared ?? 0);
}
