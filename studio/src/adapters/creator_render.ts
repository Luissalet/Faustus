import { responseReason } from './api';
import { CreatorApiError } from './creator';

/**
 * WP14 — Studio adapter over `routes/creator_render_routes.py`: compile a
 * `timeline` document + an output profile into a plan/estimate
 * (`POST /api/creator/render/plan`, no approval needed just to see it),
 * start a render (`POST /api/creator/render` — approval-gated exactly like
 * `adapters/creator.ts`'s `runPreflight`/`approvePreflight`: a 403 names
 * the digest to approve, a 409 means the plan changed since), and poll/
 * cancel it (`GET`/`POST .../cancel`, keyed by the `render_token`
 * `startRender` returns immediately — the actual `ffmpeg` run continues on
 * the server's own background thread).
 */

export interface RenderProfile {
  id: string;
  label: string;
  width: number;
  height: number;
  fps: { numerator: number; denominator: number };
  video_codec: string;
  video_bitrate: string;
  pix_fmt: string;
  audio_codec: string;
  audio_bitrate: string;
  audio_rate: number;
  container_ext: string;
}

export interface RenderPlanResponse {
  doc_id: string;
  doc_revision: number;
  profile: RenderProfile;
  graph: {
    filter_complex: string;
    video_map: string;
    audio_map: string;
    inputs: { index: number; occurrence_id: string }[];
    duration_ticks: string;
    subtitle_occurrence_id: string | null;
  };
  cache_key: string;
  cached_occurrence_id: string | null;
  estimated_duration_seconds: number;
  estimated_size_bytes: number;
  engine_build: string;
  engine_available: boolean;
  engine_reason: string;
}

export interface StartRenderResponse {
  render_token: string;
  state: string;
}

export interface RenderJobResponse {
  render_token: string;
  state: 'starting' | 'done' | 'error';
  engine_state: string;
  progress: number | null;
  ffmpeg_job_id: string | null;
  result: {
    cache_hit: boolean;
    status: string;
    job_id: string | null;
    occurrence_id: string | null;
    cache_key: string;
    artifacts: unknown[];
    reason?: string;
  } | null;
  error: string | null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail: unknown = {};
    try {
      detail = await response.json();
    } catch {
      detail = {};
    }
    const inner = detail && typeof detail === 'object' && 'detail' in (detail as Record<string, unknown>)
      ? (detail as Record<string, unknown>).detail
      : detail;
    const message = await responseReason(response, path);
    const payload = (inner && typeof inner === 'object' ? inner : {}) as Record<string, unknown>;
    throw new CreatorApiError(message, response.status, payload);
  }
  return (await response.json()) as T;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
  });
}

export function getRenderPlan(
  projectId: string, docId: string, profileId: string, subtitleOccurrenceId?: string,
): Promise<RenderPlanResponse> {
  return post('/api/creator/render/plan', {
    project_id: projectId, doc_id: docId, profile_id: profileId,
    subtitle_occurrence_id: subtitleOccurrenceId,
  });
}

/** Throws `CreatorApiError` with `status === 403` (`detail.digest` names the
 *  preflight digest to approve first) or `409` (the plan changed). */
export function startRender(
  projectId: string, docId: string, profileId: string,
  opts: { subtitleOccurrenceId?: string; preflightDigest?: string; sessionId?: string } = {},
): Promise<StartRenderResponse> {
  return post('/api/creator/render', {
    project_id: projectId, doc_id: docId, profile_id: profileId,
    subtitle_occurrence_id: opts.subtitleOccurrenceId,
    preflight_digest: opts.preflightDigest, session_id: opts.sessionId,
  });
}

export function getRenderJob(renderToken: string, signal?: AbortSignal): Promise<RenderJobResponse> {
  return request(`/api/creator/render/${encodeURIComponent(renderToken)}`, { signal });
}

export function cancelRender(renderToken: string): Promise<{ outcome: string; detail: string }> {
  return post(`/api/creator/render/${encodeURIComponent(renderToken)}/cancel`);
}
