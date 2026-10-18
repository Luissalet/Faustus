import { getJson, responseReason } from './api';
export interface ProjectTask {
  id: number; title: string; body: string; acceptance: string; status: string; priority: number;
  owner: string | null; reviewer: string | null; kind: string; etag: string; thread_id: number;
  updated: number; origin_ref?: string | null;
  origin_snapshot?: Record<string, unknown> | null;
}
export interface TaskMessage { id: number; author: string; body: string; kind: string; created: number }
export interface TaskEdit { revision: number; author: string; created: number; kind: string; before: Record<string, unknown> | null; after: Record<string, unknown> }
export interface TaskDetail { task: ProjectTask; messages: TaskMessage[]; edits?: TaskEdit[] }
export const taskStatusLabel: Record<string, string> = { open: 'Open', claimed: 'Claimed', in_progress: 'In progress', blocked: 'Blocked', changes: 'Changes requested', review: 'In review', approved: 'Approved', done: 'Done', dropped: 'Dropped' };
const base = (project: string) => `/api/projects/${encodeURIComponent(project)}/tasks`;
async function write<T>(path: string, method: string, value = {}): Promise<T> {
  const response = await fetch(path, { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
  if (!response.ok) throw new Error(await responseReason(response, path));
  const result = await response.json();
  if (result.ok === false) throw new Error(typeof result.error === 'string' ? result.error : 'Project tasks are unavailable');
  return result;
}
export const prepareTasks = (project: string) => write(base(project) + '/prepare', 'POST');
export const listProjectTasks = (project: string, signal?: AbortSignal) => getJson<{ tasks: ProjectTask[]; space_id: string | null }>(base(project), signal);
export const readProjectTask = (project: string, id: number, signal?: AbortSignal) => getJson<TaskDetail>(`${base(project)}/${id}`, signal);
export const createProjectTask = (project: string, fields: object) => write<{ task: ProjectTask }>(base(project), 'POST', fields);
export const editProjectTask = (project: string, id: number, fields: object) => write<{ task: ProjectTask }>(`${base(project)}/${id}`, 'PATCH', fields);
export const taskColumns = [
  { id: 'open', label: 'Open', statuses: ['open'] },
  { id: 'in_progress', label: 'In progress', statuses: ['claimed', 'in_progress', 'blocked', 'changes'] },
  { id: 'review', label: 'In review', statuses: ['review'] },
  { id: 'done', label: 'Done', statuses: ['approved', 'done', 'dropped'] },
] as const;
