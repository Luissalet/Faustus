import { responseReason } from './api';
import { applyCommand, CreatorApiError, RevisionConflictError, type ApplyCommandResult } from './creator';

/**
 * WP24 — Studio adapter over the Music Studio API: the typed `song.*` ops
 * (`src/creator/ops/song_ops.py`, dispatched through the SAME
 * `POST /api/creator/documents/{id}/commands` route every other Creator op
 * uses — `applyCommand`, WP02/WP05) plus the music-specific generation
 * routes (`routes/creator_music_routes.py`, WP24).
 *
 * `genCommandId()` mirrors `adapters/creator_timeline.ts`'s convention.
 */

export interface SongSection {
  id: string;
  kind: string;
  lyrics: string;
}

export interface SongTake {
  id: string;
  occurrence_id: string;
  engine: string;
  engine_version?: string;
  seed: number | null;
  bpm: number | null;
  key: string | null;
  duration_s: number | null;
  steps?: number | null;
  created_at: number;
  lyrics_timing: unknown | null;
  warnings?: string[];
  favorite: boolean;
}

export interface SongDocContent {
  language: string;
  sections: SongSection[];
  takes: SongTake[];
  selected_take: string | null;
  style_tags?: string[];
  bpm_target?: number | null;
  key_target?: string | null;
  seed?: number | null;
}

export function genCommandId(prefix = 'cmd'): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// ── song.* typed ops (WP12/WP24), dispatched via applyCommand ──────────

export function editLyrics(
  docId: string, expectedRevision: number, sectionId: string, lyrics: string,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('lyrics'), expectedRevision, {
    type: 'song.edit_lyrics', object_id: sectionId, lyrics,
  });
}

export function addSection(
  docId: string, expectedRevision: number, sectionId: string, kind: string,
  lyrics = '', atIndex?: number,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('section'), expectedRevision, {
    type: 'song.add_section', section_id: sectionId, kind, lyrics, at_index: atIndex,
  });
}

export function removeSection(
  docId: string, expectedRevision: number, sectionId: string,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('rm-section'), expectedRevision, {
    type: 'song.remove_section', object_id: sectionId,
  });
}

export function reorderSections(
  docId: string, expectedRevision: number, order: string[],
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('reorder'), expectedRevision, {
    type: 'song.reorder_sections', order,
  });
}

export function setStyle(
  docId: string, expectedRevision: number,
  opts: { styleTags?: string[]; bpmTarget?: number | null; keyTarget?: string | null },
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('style'), expectedRevision, {
    type: 'song.set_style', style_tags: opts.styleTags, bpm_target: opts.bpmTarget, key_target: opts.keyTarget,
  });
}

export function setSeed(
  docId: string, expectedRevision: number, seed: number | null,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('seed'), expectedRevision, {
    type: 'song.set_seed', seed,
  });
}

export function selectTake(
  docId: string, expectedRevision: number, takeId: string | null,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('select-take'), expectedRevision, {
    type: 'song.select_take', take_id: takeId,
  });
}

export function markTakeFavorite(
  docId: string, expectedRevision: number, takeId: string, favorite: boolean,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('fav'), expectedRevision, {
    type: 'song.mark_take_favorite', object_id: takeId, favorite,
  });
}

// ── generation routes (routes/creator_music_routes.py) ─────────────────

async function payloadOf(response: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await response.clone().json();
    return body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function musicRequest<T>(path: string, init?: RequestInit): Promise<T> {
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

export interface MusicJob {
  job_id: string;
  state: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'unknown';
  doc_id?: string;
  project_id?: string;
  occurrence_id?: string | null;
  take_id?: string | null;
  error?: string;
  created_at?: number;
}

export interface GenerateMusicRequest {
  docId: string;
  projectId?: string;
  engine?: string;
  deploymentId?: string;
  durationS?: number;
  steps?: number;
  seed?: number;
  device?: string;
  referenceOccurrenceId?: string;
  preflightDigest?: string;
}

/** Throws `CreatorApiError` with `status === 403` and
 *  `payload.reason === 'preflight_required'` (carrying `payload.digest`)
 *  when this generation needs an approved preflight first — the screen
 *  reads that to send the caller through `runPreflight`/`approvePreflight`
 *  (`adapters/creator.ts`) before retrying with the approved digest. */
export function generateMusic(req: GenerateMusicRequest): Promise<MusicJob> {
  return musicRequest('/api/creator/music/generate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      doc_id: req.docId, project_id: req.projectId, engine: req.engine,
      deployment_id: req.deploymentId, duration_s: req.durationS, steps: req.steps,
      seed: req.seed, device: req.device, reference_occurrence_id: req.referenceOccurrenceId,
      preflight_digest: req.preflightDigest,
    }),
  });
}

export function getMusicJob(jobId: string, signal?: AbortSignal): Promise<MusicJob> {
  return musicRequest(`/api/creator/music/${encodeURIComponent(jobId)}`, { signal });
}

export function cancelMusicJob(jobId: string): Promise<{ job_id: string; outcome: string; detail?: string }> {
  return musicRequest(`/api/creator/music/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
}

export function generateMusicVariants(
  docId: string, n: number, opts: Omit<GenerateMusicRequest, 'docId'> = {},
): Promise<{ jobs: MusicJob[] }> {
  return musicRequest(`/api/creator/music/${encodeURIComponent(docId)}/variants`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      n, project_id: opts.projectId, engine: opts.engine, deployment_id: opts.deploymentId,
      duration_s: opts.durationS, steps: opts.steps, seed: opts.seed, device: opts.device,
      preflight_digest: opts.preflightDigest,
    }),
  });
}

/** Polls `getMusicJob` until it reaches a terminal state, calling `onTick`
 *  after every poll (including the terminal one). Stops on unmount via
 *  `signal`. Same shape a screen would otherwise hand-roll per job. */
export async function pollMusicJob(
  jobId: string, onTick: (job: MusicJob) => void,
  opts: { intervalMs?: number; signal?: AbortSignal } = {},
): Promise<MusicJob> {
  const intervalMs = opts.intervalMs ?? 1000;
  for (;;) {
    if (opts.signal?.aborted) throw new DOMException('aborted', 'AbortError');
    const job = await getMusicJob(jobId, opts.signal);
    onTick(job);
    if (['completed', 'failed', 'cancelled', 'unknown'].includes(job.state)) return job;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}
