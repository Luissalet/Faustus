import { getJson, responseReason, ApiError } from './api';

export interface ObjectiveRef {
  kind: 'task' | 'agent' | 'resource' | 'schedule' | 'change';
  id?: string; label?: string; project_id?: string; workspace?: string; path?: string; scope?: 'staged' | 'unstaged';
}
export interface ConversationObjective {
  id: string; session_id: string; revision: number; statement: string; context: string; deliverable: string;
  refs: ObjectiveRef[]; status: 'active' | 'paused' | 'complete'; active_seconds: number; result: string;
  runs: {run_id: string; revision: number; linked_at: number; brief?: {statement: string; deliverable: string} | null; generation?: {state: string; source: string} | null}[];
}
export interface ObjectiveResponse { objective: ConversationObjective | null; generation?: {run_id: string; state: string; active: boolean} | null; pause_requested?: boolean; run_id?: string | null; pending?: boolean }
const path = (sid: string) => `/api/session/${encodeURIComponent(sid)}/objective`;
export function getObjective(sid: string, signal?: AbortSignal) { return getJson<ObjectiveResponse>(path(sid), signal); }
export async function writeObjective(sid: string, action: 'save' | 'state' | 'continue', body: unknown): Promise<ObjectiveResponse> {
  const url = path(sid) + (action === 'save' ? '' : `/${action}`);
  const response = await fetch(url, {method: action === 'save' ? 'PUT' : 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (!response.ok) throw new ApiError(await responseReason(response, url), response.status);
  return response.json() as Promise<ObjectiveResponse>;
}
