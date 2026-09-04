import { asArray, getJson } from './api';

export interface Project {
  id: string;
  name: string;
  folder?: string | null;
  workspace?: string | null;
  instructions?: string | null;
  enabled?: boolean;
  created_at?: number | null;
  updated_at?: number | null;
}

export interface MemoryFile {
  name: string;
  size: number;
  modified: number;
}

export interface ProjectMemory {
  dir: string;
  files: MemoryFile[];
}

export interface Objective {
  id?: string;
  title?: string;
  name?: string;
  status?: string;
  done?: boolean;
}

export function listProjects(signal?: AbortSignal): Promise<Project[]> {
  return getJson<unknown>('/api/projects', signal).then((value) =>
    asArray<Project>(value, 'projects'),
  );
}

export function getProject(id: string, signal?: AbortSignal): Promise<Project> {
  return getJson<Project>(`/api/projects/${encodeURIComponent(id)}`, signal);
}

export function getMemory(id: string, signal?: AbortSignal): Promise<ProjectMemory> {
  return getJson<ProjectMemory>(`/api/projects/${encodeURIComponent(id)}/memory`, signal);
}

export function getObjectives(id: string, signal?: AbortSignal): Promise<Objective[]> {
  return getJson<unknown>(`/api/projects/${encodeURIComponent(id)}/objectives`, signal).then(
    (value) => asArray<Objective>(value, 'objectives'),
  );
}

/**
 * The exact context block the model receives for this project.
 *
 * This is the endpoint the whole overhaul is arguing for: "el usuario ve qué
 * sabe y qué usará Faustus". It already existed and nothing showed it.
 */
export function getContextPreview(id: string, signal?: AbortSignal): Promise<string> {
  return getJson<{ block?: string }>(
    `/api/projects/${encodeURIComponent(id)}/preview`,
    signal,
  ).then((value) => value.block ?? '');
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
