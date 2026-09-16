import { responseReason } from './api';
import { applyCommand, CreatorApiError, RevisionConflictError, type ApplyCommandResult } from './creator';

/**
 * WP13 — Studio adapter over the timeline-specific typed ops added to
 * `src/creator/ops/timeline_ops.py` (WP12's `timeline.insert_clip`/`trim`/
 * `split`/`move`/`ripple_delete`/`set_gain`, plus WP13's additive
 * `timeline.snap`/`timeline.retime`/`timeline.add_marker`/
 * `timeline.ripple_insert`). Every call here goes through
 * `applyCommand` (`adapters/creator.ts`, WP02/WP05) — there is no separate
 * timeline endpoint, ops are dispatched by `op.type` on the SAME
 * `POST /api/creator/documents/{id}/commands` route every other Creator op
 * uses, so a stale `expected_revision` still surfaces as the one
 * `RevisionConflictError` the shell already knows how to handle.
 *
 * Ticks are always decimal strings on the wire (never a JS `number` — see
 * `src/creator/ops/model.py`'s module docstring for why), so every
 * tick-shaped parameter here is typed `string`. `genCommandId()` mirrors
 * the dedupe-key convention `CreatorScreen.tsx` already uses.
 */

export type TimelineDocContent = {
  clock: { ticks_per_second_numerator: string; ticks_per_second_denominator: string };
  duration_ticks: string;
  tracks: TimelineTrack[];
  markers?: TimelineMarker[];
};

export interface TimelineTrack {
  id: string;
  kind: 'video' | 'audio' | 'image' | 'caption' | 'overlay';
  locked: boolean;
  clips: TimelineClip[];
}

export interface TimelineClip {
  id: string;
  asset_ref: string;
  timeline_start_ticks: string;
  timeline_duration_ticks: string;
  source_range: { start_ticks: string; duration_ticks: string };
  source_clock: { ticks_per_second_numerator: string; ticks_per_second_denominator: string };
  gain_db?: number;
  retiming_map?: RetimingSegment[];
}

export interface RetimingSegment {
  source: { start_ticks: string; duration_ticks: string };
  dest: { start_ticks: string; duration_ticks: string };
  mappable?: boolean;
}

export interface TimelineMarker {
  id: string;
  at_ticks: string;
  label?: string | null;
}

export function genCommandId(prefix = 'cmd'): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// ── ops carried over unchanged from WP12 (typed op wrappers) ───────────

export function insertClip(
  docId: string, expectedRevision: number, trackId: string, clip: TimelineClip,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('insert'), expectedRevision, {
    type: 'timeline.insert_clip', track_id: trackId, clip,
  });
}

export function trimClip(
  docId: string, expectedRevision: number, trackId: string, clipId: string,
  sourceRange: { start_ticks: string; duration_ticks: string },
  opts: { timeline_duration_ticks?: string; timeline_start_ticks?: string } = {},
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('trim'), expectedRevision, {
    type: 'timeline.trim', track_id: trackId, clip_id: clipId, source_range: sourceRange, ...opts,
  });
}

export function splitClip(
  docId: string, expectedRevision: number, trackId: string, clipId: string, atTicks: string, newClipId: string,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('split'), expectedRevision, {
    type: 'timeline.split', track_id: trackId, clip_id: clipId, at_ticks: atTicks, new_clip_id: newClipId,
  });
}

export function moveClip(
  docId: string, expectedRevision: number, trackId: string, clipId: string,
  timelineStartTicks: string, targetTrackId?: string,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('move'), expectedRevision, {
    type: 'timeline.move', track_id: trackId, clip_id: clipId,
    timeline_start_ticks: timelineStartTicks, target_track_id: targetTrackId,
  });
}

export function rippleDelete(
  docId: string, expectedRevision: number, trackId: string, clipId: string,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('ripple-del'), expectedRevision, {
    type: 'timeline.ripple_delete', track_id: trackId, clip_id: clipId,
  });
}

export function setGain(
  docId: string, expectedRevision: number, trackId: string, clipId: string, gainDb: number,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('gain'), expectedRevision, {
    type: 'timeline.set_gain', track_id: trackId, clip_id: clipId, gain_db: gainDb,
  });
}

// ── WP13 additive ops ───────────────────────────────────────────────────

export function snapClip(
  docId: string, expectedRevision: number, trackId: string, clipId: string,
  nearTicks: string, windowTicks: string, opts: { frameTicks?: string; includeTracks?: boolean; includeMarkers?: boolean } = {},
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('snap'), expectedRevision, {
    type: 'timeline.snap', track_id: trackId, clip_id: clipId,
    near_ticks: nearTicks, window_ticks: windowTicks,
    frame_ticks: opts.frameTicks, include_tracks: opts.includeTracks, include_markers: opts.includeMarkers,
  });
}

export function retimeClip(
  docId: string, expectedRevision: number, trackId: string, clipId: string, retimingMap: RetimingSegment[],
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('retime'), expectedRevision, {
    type: 'timeline.retime', track_id: trackId, clip_id: clipId, retiming_map: retimingMap,
  });
}

export function addMarker(
  docId: string, expectedRevision: number, markerId: string, atTicks: string, label?: string,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('marker'), expectedRevision, {
    type: 'timeline.add_marker', marker_id: markerId, at_ticks: atTicks, label,
  });
}

export function rippleInsert(
  docId: string, expectedRevision: number, trackId: string, clip: TimelineClip,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('ripple-ins'), expectedRevision, {
    type: 'timeline.ripple_insert', track_id: trackId, clip,
  });
}

// ── client-side helpers (mirror src/creator/timeline/*.py — advisory only,
// the server always revalidates on write) ──────────────────────────────

export function clipEndTicks(clip: TimelineClip): bigint {
  return BigInt(clip.timeline_start_ticks) + BigInt(clip.timeline_duration_ticks);
}

/** Every clip boundary tick on a track, sorted — mirrors
 *  `src/creator/timeline/tracks.py::cut_points`, used for the client-side
 *  drag-snap preview before a `timeline.snap` command is sent. */
export function cutPoints(track: TimelineTrack): bigint[] {
  const points = new Set<bigint>();
  for (const clip of track.clips) {
    points.add(BigInt(clip.timeline_start_ticks));
    points.add(clipEndTicks(clip));
  }
  return Array.from(points).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

// ── read routes added by WP13 (routes/creator_timeline_routes.py) ──────

async function payloadOf(response: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await response.clone().json();
    return body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function tlRequest<T>(path: string, init?: RequestInit): Promise<T> {
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
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('text/plain')) return (await response.text()) as unknown as T;
  return (await response.json()) as T;
}

export interface TimelineProjectView {
  clock: { ticks_per_second_numerator: string; ticks_per_second_denominator: string };
  duration_ticks: string;
  tracks: {
    id: string; kind: string; locked: boolean;
    clips: { id: string; asset_ref: string; start_ticks: string; end_ticks: string; duration_ticks: string; gain_db?: number | null }[];
    span: { start_ticks: string; end_ticks: string } | null;
  }[];
  markers: { id: string; at_ticks: string; label?: string | null }[];
}

export function getTimelineView(docId: string, signal?: AbortSignal): Promise<{ revision: number; view: TimelineProjectView }> {
  return tlRequest(`/api/creator/documents/${encodeURIComponent(docId)}/timeline/view`, { signal });
}

export interface ValidationFinding {
  severity: 'error' | 'warning';
  code: string;
  message: string;
  target?: string;
}

export function getTimelineValidation(docId: string, signal?: AbortSignal): Promise<{ revision: number; ok: boolean; findings: ValidationFinding[] }> {
  return tlRequest(`/api/creator/documents/${encodeURIComponent(docId)}/timeline/validate`, { signal });
}

export function getTimelineEdl(docId: string, trackId?: string): Promise<string> {
  const qs = trackId ? `?track_id=${encodeURIComponent(trackId)}` : '';
  return tlRequest(`/api/creator/documents/${encodeURIComponent(docId)}/timeline/edl${qs}`);
}

export function getRevisionSnapshot(
  docId: string, revision: number, signal?: AbortSignal,
): Promise<{ revision: number; state: string; content: TimelineDocContent; asset_refs: string[]; at: number }> {
  return tlRequest(`/api/creator/documents/${encodeURIComponent(docId)}/revisions/${revision}`, { signal });
}

/** Undo/redo as a NEW revision (`routes/creator_timeline_routes.py::undo_document`,
 *  wrapping `src/creator/ops/undo.py::undo_to`). "Redo" is the same call with a
 *  LATER `targetRevision` — this function does not distinguish direction. */
export function undoTo(
  docId: string, targetRevision: number, expectedRevision: number, commandId = genCommandId('undo'),
): Promise<ApplyCommandResult> {
  return tlRequest(`/api/creator/documents/${encodeURIComponent(docId)}/undo`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ command_id: commandId, target_revision: targetRevision, expected_revision: expectedRevision }),
  });
}

export interface SnapPreview {
  ticks: bigint;
  source: 'cut' | 'marker' | 'frame';
}

/** Nearest snap target to `near` within `window` ticks, mirroring
 *  `src/creator/timeline/snapping.py::snap`'s priority (cut/marker beats
 *  the frame grid). Pure preview: the actual write goes through
 *  `snapClip`, which re-derives this server-side and is authoritative. */
export function previewSnap(
  content: TimelineDocContent, near: bigint, window: bigint,
  opts: { frameTicks?: bigint; excludeTrackId?: string } = {},
): SnapPreview | null {
  const lo = near - window;
  const hi = near + window;
  let best: SnapPreview | null = null;
  let bestDist: bigint | null = null;
  const consider = (ticks: bigint, source: SnapPreview['source']) => {
    if (ticks < lo || ticks > hi) return;
    const dist = ticks > near ? ticks - near : near - ticks;
    const rank = source === 'frame' ? 1 : 0;
    const bestRank = best && best.source === 'frame' ? 1 : 0;
    if (best === null || rank < bestRank || (rank === bestRank && (bestDist === null || dist < bestDist))) {
      best = { ticks, source };
      bestDist = dist;
    }
  };
  for (const track of content.tracks) {
    if (track.id === opts.excludeTrackId) continue;
    for (const p of cutPoints(track)) consider(p, 'cut');
  }
  for (const m of content.markers ?? []) consider(BigInt(m.at_ticks), 'marker');
  if (opts.frameTicks && opts.frameTicks > 0n) {
    const frame = opts.frameTicks;
    let t = (lo / frame) * frame;
    if (t < 0n) t = 0n;
    while (t <= hi) {
      consider(t, 'frame');
      t += frame;
    }
  }
  return best;
}
