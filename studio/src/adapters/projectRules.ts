import { ApiError, getJson } from './api';

/**
 * Project rules (`src/project_rules.py`, `routes/project_rules_routes.py`)
 * — per-project rule files discovered on disk (`.faustus/rules`, `.cursor/
 * rules`, …) plus a bundled library of rules any project can install from.
 */

export interface ProjectRule {
  id: string;
  origin: string;
  root: string;
  path: string;
  distance: number;
  bytes: number;
  error: string;
  text: string;
}

export interface LibraryRule {
  id: string;
  area: string;
  topic: string;
  title: string;
  applies_to: string[];
  priority: number;
  summary: string;
  body: string;
}

export interface ProjectRulesData {
  workspace: string;
  languages: string[];
  trusted: boolean;
  project_rules: ProjectRule[];
  block: string;
}

async function readError(response: Response, path: string): Promise<string> {
  try {
    const body = (await response.clone().json()) as { detail?: unknown };
    if (typeof body?.detail === 'string' && body.detail.trim()) return body.detail;
  } catch {
    /* not JSON */
  }
  return `${path} responded ${response.status}`;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(await readError(response, path), response.status);
  return (await response.json()) as T;
}

export async function getLibraryRules(): Promise<LibraryRule[]> {
  const data = await getJson<{ rules: LibraryRule[]; count: number }>('/api/rules/library');
  return data.rules ?? [];
}

export function getProjectRules(workspace: string): Promise<ProjectRulesData> {
  return getJson<ProjectRulesData>(`/api/rules?workspace=${encodeURIComponent(workspace)}`);
}

export interface RuleActionRow {
  id: string;
  status: 'installed' | 'not_found' | 'refused' | 'error' | 'removed' | 'not_installed';
  path?: string;
  reason?: string;
}

export function installProjectRules(workspace: string, ids: string[]): Promise<{ results: RuleActionRow[] }> {
  return postJson('/api/rules/install', { workspace, ids });
}

export function uninstallProjectRules(workspace: string, ids: string[]): Promise<{ results: RuleActionRow[] }> {
  return postJson('/api/rules/uninstall', { workspace, ids });
}
