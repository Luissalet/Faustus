import { responseReason } from './api';
import {
  applyCommand, CreatorApiError, RevisionConflictError,
  type ApplyCommandResult, type CreatorDocument,
} from './creator';

/**
 * WP16 — Studio adapter over `routes/creator_subtitle_routes.py`
 * (from-transcript / qa / export) and the typed `subtitles.*` ops
 * `src/creator/ops/subtitle_ops.py` registers onto the SAME
 * `POST /api/creator/documents/{id}/commands` route every other Creator op
 * uses (see `adapters/creator_timeline.ts`'s own docstring for why edits
 * go through that one route rather than a bespoke endpoint per op).
 *
 * Ticks are decimal strings on the wire (never a JS `number`), same rule
 * `src/creator/ops/model.py` documents.
 */

export interface SubtitleProfile {
  max_chars_per_line: number;
  max_lines: number;
  cps_max: number;
  min_duration_seconds: number;
  max_duration_seconds: number;
  min_gap_seconds: number;
  pause_break_seconds: number;
}

export interface SubtitleStyle {
  font_family: string;
  font_size: number;
  primary_color: string;
  outline_color: string;
  back_color: string;
  bold: boolean;
  italic: boolean;
  shadow: number;
  alignment: number;
  position: string;
  margin_l: number;
  margin_r: number;
  margin_v: number;
}

export interface SubtitleCue {
  id: string;
  start_ticks: string;
  duration_ticks: string;
  lines: string[];
  speaker_id: string | null;
  source_cue_ids: string[];
  editorial_status: 'proposed' | 'accepted' | 'needs_review';
  edited: boolean;
  unmapped: boolean;
  estimated_timing: boolean;
  style_id: string | null;
}

export interface SubtitlesDocContent {
  language: string;
  clock: { ticks_per_second_numerator: string; ticks_per_second_denominator: string };
  profile: SubtitleProfile;
  style: SubtitleStyle;
  styles: Record<string, SubtitleStyle>;
  cues: SubtitleCue[];
  source_transcript_id?: string;
}

export interface QaIssue { rule: string; actual: unknown; target: unknown; line?: string }
export interface QaCueIssues { cue_id: string; start_ticks: string; issues: QaIssue[] }
export interface QaReport { total_cues: number; cues_with_issues: number; issues: QaCueIssues[] }

export interface RetimingSegment {
  source: { start_ticks: string; duration_ticks: string };
  dest: { start_ticks: string; duration_ticks: string };
  mappable?: boolean;
}

export function genCommandId(prefix = 'sub'): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

async function payloadOf(response: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await response.clone().json();
    return body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function sReq<T>(path: string, init?: RequestInit): Promise<T> {
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
  return (await response.json()) as T;
}

// ── from-transcript / qa / export (routes/creator_subtitle_routes.py) ──

export function createFromTranscript(
  transcriptDocId: string,
  opts: { language?: string; profileOverrides?: Partial<SubtitleProfile>; style?: Partial<SubtitleStyle> } = {},
): Promise<CreatorDocument> {
  return sReq('/api/creator/subtitles/from-transcript', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      transcript_doc_id: transcriptDocId, language: opts.language,
      profile_overrides: opts.profileOverrides, style: opts.style,
    }),
  });
}

export function getQa(docId: string, signal?: AbortSignal): Promise<{ doc_id: string; revision: number; report: QaReport }> {
  return sReq(`/api/creator/subtitles/${encodeURIComponent(docId)}/qa`, { signal });
}

/** A direct download URL — the browser handles the `Content-Disposition`
 *  attachment; nothing here parses the file, it just builds the request
 *  the export route (`Response(...)`, `routes/creator_subtitle_routes.py`)
 *  answers. */
export function exportUrl(docId: string, format: 'srt' | 'vtt' | 'ass'): string {
  return `/api/creator/subtitles/${encodeURIComponent(docId)}/export?format=${format}`;
}

export async function fetchExportText(docId: string, format: 'srt' | 'vtt' | 'ass'): Promise<string> {
  const response = await fetch(exportUrl(docId, format), { credentials: 'same-origin' });
  if (!response.ok) {
    const message = await responseReason(response, exportUrl(docId, format));
    throw new CreatorApiError(message, response.status, await payloadOf(response));
  }
  return response.text();
}

// ── typed ops (src/creator/ops/subtitle_ops.py), via applyCommand ──────

export function editCue(
  docId: string, expectedRevision: number, cueId: string,
  changes: {
    lines?: string[]; start_ticks?: string; duration_ticks?: string;
    speaker_id?: string | null; style_id?: string | null;
    editorial_status?: 'proposed' | 'accepted' | 'needs_review';
  },
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('edit'), expectedRevision, {
    type: 'subtitles.edit_cue', object_id: cueId, ...changes,
  });
}

export function splitCue(
  docId: string, expectedRevision: number, cueId: string, atTicks: string,
  firstLines: string[], secondLines: string[], newCueId: string,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('split'), expectedRevision, {
    type: 'subtitles.split_cue', object_id: cueId, at_ticks: atTicks,
    first_lines: firstLines, second_lines: secondLines, new_cue_id: newCueId,
  });
}

export function mergeCues(
  docId: string, expectedRevision: number, firstCueId: string, secondCueId: string, lines: string[],
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('merge'), expectedRevision, {
    type: 'subtitles.merge_cues', first_object_id: firstCueId, second_object_id: secondCueId, lines,
  });
}

export function shiftCues(
  docId: string, expectedRevision: number, offsetTicks: number, cueIds?: string[],
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('shift'), expectedRevision, {
    type: 'subtitles.shift_cues', offset_ticks: offsetTicks, object_ids: cueIds,
  });
}

export function setStyle(
  docId: string, expectedRevision: number, style: Partial<SubtitleStyle>, styleId?: string,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('style'), expectedRevision, {
    type: 'subtitles.set_style', style, style_id: styleId,
  });
}

export function applyRetiming(
  docId: string, expectedRevision: number, retiming: RetimingSegment[],
  direction: 'source_to_dest' | 'dest_to_source' = 'source_to_dest',
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('retime'), expectedRevision, {
    type: 'subtitles.apply_retiming', retiming, direction,
  });
}

export function regenerateFromTranscript(
  docId: string, expectedRevision: number, transcriptContent: unknown,
  profileOverrides?: Partial<SubtitleProfile>,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('regen'), expectedRevision, {
    type: 'subtitles.regenerate_from_transcript',
    transcript_content: transcriptContent, profile_overrides: profileOverrides,
  });
}
