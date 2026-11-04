import { ApiError, getJson } from './api';

/* Design canvas history (Darwin #989, routes/design_canvas_routes.py):
   every filing of a design canvas is a numbered version; the diff is per
   element, and decisions (who, what, why) hang off a version. */

export interface CanvasRow { id: string; name: string; project_id: string; versions: number; latest: number; decisions: number; updated: number | null; goal: string }
export interface CanvasVersion { version: number; ts: number; author: string; source?: string; goal: string; note: string; summary?: string }
export interface CanvasDecision { id: string; version: number; ts: number; author: string; decision: string; why: string; kind: string }
export interface CanvasHistory { id: string; name: string; project_id: string; versions: CanvasVersion[]; decisions: CanvasDecision[] }
export interface CanvasChange {
  kind: 'added' | 'removed' | 'changed' | 'moved' | 'reordered';
  dimension?: string; text?: string; before?: string; after?: string; similarity?: number;
  from_dimension?: string; to_dimension?: string; from_index?: number; to_index?: number;
  sentences_added?: string[]; sentences_removed?: string[];
}
export interface CanvasDiff {
  changes: CanvasChange[]; counts: Record<string, number>; identical: boolean;
  from: CanvasVersion; to: CanvasVersion; decisions: CanvasDecision[]; text: string;
}

export async function listCanvases(projectId = '', signal?: AbortSignal): Promise<CanvasRow[]> {
  const body = await getJson<{ canvases?: CanvasRow[] }>(`/api/design-canvas?project_id=${encodeURIComponent(projectId)}`, signal);
  return Array.isArray(body.canvases) ? body.canvases : [];
}

export function canvasHistory(id: string, signal?: AbortSignal): Promise<CanvasHistory> {
  return getJson<CanvasHistory>(`/api/design-canvas/${encodeURIComponent(id)}`, signal);
}

export function canvasDiff(id: string, from?: number, to?: number, signal?: AbortSignal): Promise<CanvasDiff> {
  const q = new URLSearchParams();
  if (from) q.set('from', String(from));
  if (to) q.set('to', String(to));
  return getJson<CanvasDiff>(`/api/design-canvas/${encodeURIComponent(id)}/diff${q.toString() ? `?${q}` : ''}`, signal);
}

export async function addCanvasDecision(id: string, entry: { decision: string; why?: string; version?: number }): Promise<CanvasDecision> {
  const response = await fetch(`/api/design-canvas/${encodeURIComponent(id)}/decisions`, {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(entry),
  });
  if (!response.ok) throw new ApiError(`decision responded ${response.status}`, response.status);
  return (await response.json()) as CanvasDecision;
}
