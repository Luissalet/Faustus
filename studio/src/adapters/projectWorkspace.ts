import { ApiError } from './api';

export interface ProjectSkill {
  key: string; name: string; description?: string; markdown?: string; digest: string;
  active: boolean; changed?: boolean; error?: string; tools?: string[];
  files?: Record<string, string>;
  review?: { permissions?: Record<string, unknown>; privilege_request: string[]; security_scan?: { risk_level: string; findings: { severity: string; file: string; line: number; description: string }[] }; status: { state: string } };
}
export interface CatalogSkill { id: string; kind: string; name: string; description: string }
export interface RepoSkill { name: string; path: string; url: string; revision: string }

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new ApiError(typeof data.detail === 'string' ? data.detail : data.error || `Request failed (${response.status})`, response.status);
  return data as T;
}
export function workspaceAction<T>(projectId: string, action: string, args: Record<string, unknown> = {}) {
  return post<T>(`/api/projects/${encodeURIComponent(projectId)}/workspace/action`, { action, ...args });
}
export function reviewActivateSkill(projectId: string, skill: ProjectSkill, override: boolean) {
  return post(`/api/projects/${encodeURIComponent(projectId)}/workspace/skills/review-activate`, { key: skill.key, digest: skill.digest, override });
}

export async function skillFiles(files: File[]): Promise<Record<string, string>> {
  if (!files.length || files.length > 64 || files.reduce((sum, f) => sum + f.size, 0) > 2_000_000 || files.some((f) => f.size > 400_000)) throw new Error('Choose up to 64 text files, at most 400 KB each and 2 MB in total.');
  const result: Record<string, string> = {};
  for (const file of files) {
    const name = file.webkitRelativePath || file.name;
    if (Object.hasOwn(result, name)) throw new Error('Duplicate file name in skill bundle.');
    result[name] = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
  }
  return result;
}
