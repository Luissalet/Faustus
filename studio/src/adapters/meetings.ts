import { ApiError, getJson } from './api';
import { t } from '../i18n';

/**
 * Meeting notes (`/api/meetings/*`): record or upload an audio file,
 * transcribe it in chunks with the deterministic Whisper-hallucination
 * cleanup (src/stt_cleanup.py) always applied, then one local-model pass
 * turns the cleaned transcript into Markdown notes. A finished note is a
 * Markdown file plus a JSON sidecar under DATA_DIR/meetings/.
 */
export interface MeetingSummary {
  id: string;
  title: string;
  date: string;
  created_at?: string;
  duration_seconds?: number;
  language?: string;
  stt_provider?: string;
  model_ok?: boolean;
  warnings?: string[];
  project_id?: string | null;
  source_filename?: string;
}

export interface MeetingDetail extends MeetingSummary {
  markdown: string;
}

export interface MeetingJob {
  status: 'queued' | 'running' | 'done' | 'failed';
  meeting_id?: string | null;
  error?: string | null;
  warnings?: string[];
}

async function postForm<T>(path: string, fd: FormData): Promise<T> {
  const res = await fetch(path, { method: 'POST', credentials: 'same-origin', body: fd });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: unknown };
      if (typeof j.detail === 'string') detail = j.detail;
      else if (j.detail && typeof j.detail === 'object' && 'message' in (j.detail as Record<string, unknown>)) {
        detail = String((j.detail as Record<string, unknown>).message);
      }
    } catch {
      /* not json */
    }
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

export async function listMeetings(signal?: AbortSignal): Promise<MeetingSummary[]> {
  const out = await getJson<{ meetings: MeetingSummary[] }>('/api/meetings', signal);
  return out.meetings ?? [];
}

export async function getMeeting(id: string, signal?: AbortSignal): Promise<MeetingDetail> {
  return getJson<MeetingDetail>(`/api/meetings/${encodeURIComponent(id)}`, signal);
}

export async function uploadMeeting(file: Blob, filename: string, opts?: { title?: string; language?: string; projectId?: string }): Promise<{ job_id: string }> {
  const fd = new FormData();
  fd.append('file', file, filename);
  if (opts?.title) fd.append('title', opts.title);
  if (opts?.language) fd.append('language', opts.language);
  if (opts?.projectId) fd.append('project_id', opts.projectId);
  return postForm<{ job_id: string }>('/api/meetings', fd);
}

export async function meetingJobStatus(jobId: string, signal?: AbortSignal): Promise<MeetingJob> {
  return getJson<MeetingJob>(`/api/meetings/jobs/${encodeURIComponent(jobId)}`, signal);
}

/** Polls a just-created job until it finishes (done/failed), or times out. */
export async function waitForMeetingJob(jobId: string, { intervalMs = 2000, timeoutMs = 30 * 60 * 1000 }: { intervalMs?: number; timeoutMs?: number } = {}): Promise<MeetingJob> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const job = await meetingJobStatus(jobId);
    if (job.status === 'done' || job.status === 'failed') return job;
    if (Date.now() > deadline) throw new Error(t('The meeting is taking longer than expected. Check back in Library later.'));
    await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
  }
}

export function formatMeetingDuration(seconds?: number): string {
  if (!seconds || seconds <= 0) return '';
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}
