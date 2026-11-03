import { ApiError, getJson } from './api';

/**
 * Code floorplan (`routes/code_graph_routes.py` /api/code-graph/floorplan*):
 * the plain-language map of a workspace — parts, files, routes and outgoing
 * requests — its HTML export and the blueprint check of a goal text.
 */

export interface FloorplanRoute { method: string; path: string; line: number }
export interface FloorplanRequest { method: string; url: string; line: number }
export interface FloorplanFile {
  path: string; language: string; label: string; label_source: string;
  symbols: string[]; routes: FloorplanRoute[]; requests: FloorplanRequest[];
}
export interface FloorplanPart {
  id: string; name: string; label: string; label_source: string;
  parent_name: string; test_count: number; files: FloorplanFile[];
}
export interface BlueprintMatch { part: string; part_id: string; score: number; files: string[] }
export interface BlueprintPart {
  name: string; description: string; status: 'exists' | 'partial' | 'missing';
  score: number; matches: BlueprintMatch[]; judged_by?: string; why?: string;
}
export interface Blueprint {
  goal: string; source: string; summary: Record<string, number>; parts: BlueprintPart[];
}
export interface FloorplanStats {
  parts: number; files: number; routes: number; requests: number;
  labelled_now: number; reused_labels: number; pending_labels: number;
}
export interface Floorplan {
  root: string; generated_at: string; model: string; label_note: string;
  stats: FloorplanStats; parts: FloorplanPart[]; blueprint?: Blueprint;
}

const enc = encodeURIComponent;

async function send(path: string, body: unknown): Promise<Record<string, unknown>> {
  const response = await fetch(path, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = '';
    try { detail = String(((await response.json()) as { detail?: unknown }).detail ?? ''); } catch { detail = ''; }
    throw new ApiError(detail || `${path} responded ${response.status}`, response.status);
  }
  return (await response.json()) as Record<string, unknown>;
}

/** The last floorplan of this workspace, or null when none was generated yet. */
export async function openFloorplan(root: string, signal?: AbortSignal): Promise<Floorplan | null> {
  const raw = await getJson<{ exit_code?: number; plan?: Floorplan }>(`/api/code-graph/floorplan?root=${enc(root)}`, signal);
  return raw.exit_code === 0 && raw.plan ? raw.plan : null;
}

export async function generateFloorplan(root: string, labels: 'model' | 'auto' | 'off', maxLabels = 60): Promise<Floorplan> {
  const raw = await send('/api/code-graph/floorplan', { root, labels, max_labels: maxLabels });
  if (raw.exit_code !== 0 || !raw.plan) throw new ApiError(String(raw.error || raw.output || 'Floorplan failed'), 500);
  return raw.plan as Floorplan;
}

export async function floorplanBlueprint(root: string, goal: string, labels: 'model' | 'auto' | 'off'): Promise<Blueprint> {
  const raw = await send('/api/code-graph/floorplan/blueprint', { root, goal, labels });
  if (raw.exit_code !== 0 || !raw.blueprint) throw new ApiError(String(raw.error || raw.output || 'Blueprint failed'), 500);
  return raw.blueprint as Blueprint;
}

export function floorplanExportUrl(root: string, download: boolean): string {
  return `/api/code-graph/floorplan/export?root=${enc(root)}${download ? '&download=true' : ''}`;
}

export function studioFileHref(workspace: string, path: string): string {
  return `/studio?workspace=${enc(workspace)}&file=${enc(path)}`;
}
