import { ApiError } from './api';

/**
 * Annotations and guided review (src/annotations.py, routes/annotation_routes.py): notes on the agent's plan,
 * a workspace file, a Studio document, a local web page or a review diff, kept per chat and sent back to the
 * agent as one message that quotes what each note is about.
 */

export type AnnotationType = 'comment' | 'suggestion' | 'delete' | 'question';
export type TargetKind = 'plan' | 'file' | 'doc' | 'web' | 'diff' | 'text';

export interface AnnotateTarget { kind: TargetKind; ref: string; title: string; workspace?: string }
export interface AnnotationAnchor { selector?: string; file?: string; line?: number; side?: 'new' | 'old'; hunk?: string; url?: string }
export interface Annotation {
  id: string;
  target: AnnotateTarget;
  type: AnnotationType;
  quote: string;
  text: string;
  anchor: AnnotationAnchor;
  author: string;
  created_at: number;
  sent_at: number | null;
}
export interface AnnotationsState { target: AnnotateTarget | null; annotations: Annotation[]; pending: number }
export interface Framable { url: string; origin: string; upstream: string }

export interface DiffLine { k: 'add' | 'del' | 'ctx'; old: number | null; new: number | null; text: string }
export interface DiffHunk { id: string; header: string; context: string; lines: DiffLine[] }
export interface DiffFile { path: string; status: string; added: number; removed: number; hunks: DiffHunk[] }
export interface GuideChapter { kind: 'core' | 'effects' | 'glue'; title: string; summary: string; hunks: string[] }
export interface ReviewGuide { overview: string; chapters: GuideChapter[]; model?: string; at?: number }
export interface Review {
  workspace: string;
  base: string;
  truncated: boolean;
  files: DiffFile[];
  guide?: ReviewGuide | null;
  stats: { files: number; added: number; removed: number; hunks: number };
}

async function call<T>(path: string, init: RequestInit = {}, timeout = 30000): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    signal: AbortSignal.timeout(timeout),
    ...init,
    headers: init.body ? { 'Content-Type': 'application/json', ...(init.headers || {}) } : init.headers,
  });
  let data: unknown = null;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    const detail = (data as { error?: unknown; detail?: unknown } | null);
    throw new ApiError(String(detail?.error ?? detail?.detail ?? `${path} responded ${response.status}`), response.status);
  }
  return data as T;
}
const post = <T,>(path: string, body: unknown, timeout?: number) => call<T>(path, { method: 'POST', body: JSON.stringify(body) }, timeout);
const q = (session: string) => 'session=' + encodeURIComponent(session);

export const getAnnotations = (session: string) => call<AnnotationsState>('/api/annotations?' + q(session));
export const addAnnotation = (session: string, body: { target: AnnotateTarget; type: AnnotationType; quote: string; text: string; anchor?: AnnotationAnchor }) =>
  post<Annotation>('/api/annotations', { session, ...body });
export const updateAnnotation = (session: string, id: string, body: { text?: string; type?: AnnotationType; pending?: boolean }) =>
  call<Annotation>('/api/annotations/' + encodeURIComponent(id), { method: 'PATCH', body: JSON.stringify({ session, ...body }) });
export const deleteAnnotation = (session: string, id: string) =>
  call<{ deleted: number }>(`/api/annotations/${encodeURIComponent(id)}?${q(session)}`, { method: 'DELETE' });
export const clearAnnotations = (session: string, target?: AnnotateTarget, sentOnly = false) =>
  post<{ deleted: number }>('/api/annotations/clear', { session, target, sent_only: sentOnly });
export const composeAnnotations = (session: string, ids?: string[]) =>
  post<{ text: string; count: number; ids: string[] }>('/api/annotations/compose', { session, ids });
export const openTarget = (session: string, target: AnnotateTarget) =>
  post<{ target: AnnotateTarget; web?: Framable }>('/api/annotations/open', { session, target });
export const framePage = (body: { url?: string; path?: string; html?: string; name?: string }) => post<Framable>('/api/annotations/web', body);
export const openReview = (session: string, workspace: string, base: string) =>
  post<Review>('/api/annotations/review', { session, workspace, base }, 120000);
export const getReview = (session: string) => call<Review>('/api/annotations/review?' + q(session));
export const guideReview = (session: string, model?: string) =>
  post<ReviewGuide>('/api/annotations/review/guide', { session, model }, 240000);

/** A plan's title: its first non-empty line without Markdown marks, at most 80 characters. */
export function planTitle(text: string): string {
  const line = text.split('\n').map(l => l.replace(/^#+\s*/, '').replace(/[*_`]+/g, '').trim()).find(Boolean) || '';
  return line.length > 80 ? line.slice(0, 79) + '…' : line;
}

/** Same key the server uses: notes belong to a target by kind and ref. */
export const targetKey = (t: Pick<AnnotateTarget, 'kind' | 'ref'>) => `${t.kind}:${t.ref || ''}`;

export const TYPE_LABEL: Record<AnnotationType, string> = {
  comment: 'Comment',
  suggestion: 'Suggest a change',
  delete: 'Delete',
  question: 'Question',
};
