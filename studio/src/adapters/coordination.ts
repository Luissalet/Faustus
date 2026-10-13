import { ApiError, CLIENT_VERSION_HEADER, CLIENT_API_VERSION, getJson, responseReason } from './api';

/** The product owns identity, grants and cursors. These shapes only present its state. */
export interface CoordinationTemplate {
  id: string;
  project_id: string;
  connector_id: string;
  actor: string;
  operations: string[];
  resources: string[];
  repos: string[];
  thread_ids: number[];
  revision: number;
  active: boolean;
}
export interface CoordinationBinding extends CoordinationTemplate {
  session_id: string;
  template_id?: string;
  template_revision?: number;
}
export interface CoordinationView {
  template: CoordinationTemplate | null;
  binding?: CoordinationBinding | null;
  usable: boolean;
  unavailable: string | null;
  pending?: unknown[];
  cursor?: number;
}
export interface CoordinationChoice {
  label: string;
  repo: string;
  resources: string[];
  path: string;
}
export interface CoordinationChoices {
  choices: CoordinationChoice[];
  unavailable: string | null;
}
export type CoordinationState = 'not_configured' | 'disabled' | 'unavailable' | 'prepared' | 'available';

export function coordinationState(view: CoordinationView): CoordinationState {
  if (view.template?.active === false) return 'disabled';
  const saved = view.binding ?? view.template;
  if (!saved) return 'not_configured';
  if (!saved.active) return 'disabled';
  if (view.unavailable) return 'unavailable';
  return view.usable ? 'available' : 'prepared';
}

const projectPath = (id: string) => `/api/coordination/projects/${encodeURIComponent(id)}`;
export const getProjectCoordination = (id: string, signal?: AbortSignal) => getJson<CoordinationView>(projectPath(id), signal);
export const getSessionCoordination = (id: string, signal?: AbortSignal) => getJson<CoordinationView>(`/api/coordination/${encodeURIComponent(id)}`, signal);
export const getCoordinationChoices = (project: string, connector: string, signal?: AbortSignal) =>
  getJson<CoordinationChoices>(`${projectPath(project)}/choices?connector_id=${encodeURIComponent(connector)}`, signal);

export function choiceKey(choice: CoordinationChoice): string {
  return JSON.stringify([choice.repo.toLowerCase(), choice.path.toLowerCase(), [...choice.resources].sort()]);
}
export function choiceSelected(template: CoordinationTemplate | null, choice: CoordinationChoice): boolean {
  return Boolean(template && template.repos.some(repo => repo.toLowerCase() === choice.repo.toLowerCase())
    && choice.resources.every(resource => template.resources.some(saved => saved.toLowerCase() === resource.toLowerCase())));
}

/** Keep settings outside the edited folder choices, including narrower saved scopes and model leases. */
export function coordinationDraft(template: CoordinationTemplate | null, connector: string,
  choices: CoordinationChoice[], selected: Set<string>, active: boolean) {
  const sameConnector = template?.connector_id === connector;
  const editableResources = new Set(choices.flatMap(choice => choice.resources.map(resource => resource.toLowerCase())));
  const editableRepos = new Set(choices.map(choice => choice.repo.toLowerCase()));
  const rows = choices.filter(choice => selected.has(choiceKey(choice)));
  const original = new Set(choices.filter(choice => choiceSelected(template, choice)).map(choiceKey));
  const unchanged = sameConnector && original.size === selected.size && [...original].every(key => selected.has(key));
  const retainedResources = sameConnector ? template?.resources.filter(resource => !editableResources.has(resource.toLowerCase())) ?? [] : [];
  const resources = [...new Set([
    ...retainedResources,
    ...rows.flatMap(choice => choice.resources),
  ])];
  const repos = [...new Set([
    ...(sameConnector ? template?.repos.filter(repo => !editableRepos.has(repo.toLowerCase())
      || retainedResources.some(resource => resource.toLowerCase().startsWith(`path:${repo.toLowerCase()}/`))) ?? [] : []),
    ...rows.map(choice => choice.repo),
  ])];
  return {
    connector_id: connector,
    resources: unchanged && template ? [...template.resources] : resources,
    repos: unchanged && template ? [...template.repos] : repos,
    thread_ids: [...new Set([...(template?.thread_ids ?? []), 32])],
    active,
    ...(template ? { id: template.id, expected_revision: template.revision, operations: [...template.operations] } : {}),
  };
}

export async function saveProjectCoordination(id: string, draft: ReturnType<typeof coordinationDraft>) {
  const path = projectPath(id);
  const response = await fetch(path, { method: 'PUT', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION }, body: JSON.stringify(draft) });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return await response.json() as { template: CoordinationTemplate };
}



export type CoordinationSpace = {id: string; title: string; description: string; revision: number; members: string[]};
export type CoordinationRoom = {id: string; kind: 'council' | 'groups'; title: string; space_id: string; owner: string; mode: string; status: string};

export async function spaces(signal?: AbortSignal): Promise<CoordinationSpace[]> {
  const result = await getJson<{ok: boolean; spaces?: CoordinationSpace[]; error?: string}>('/api/hoard/agora/spaces', signal);
  if (!result.ok) throw new Error(result.error || 'Coordination unavailable');
  return result.spaces || [];
}

export async function rooms(spaceId: string, signal?: AbortSignal): Promise<CoordinationRoom[]> {
  const result = await getJson<{ok: boolean; rooms?: CoordinationRoom[]; error?: string}>(
    '/api/hoard/agora/rooms?space_id=' + encodeURIComponent(spaceId), signal);
  if (!result.ok) throw new Error(result.error || 'Coordination unavailable');
  return result.rooms || [];
}

export async function createSpace(title: string): Promise<CoordinationSpace> {
  const response = await fetch('/api/hoard/agora/spaces', {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({title})});
  if (!response.ok) throw new Error(await responseReason(response, 'Coordination'));
  const result = await response.json() as {ok: boolean; space: CoordinationSpace; error?: string};
  if (!result.ok) throw new Error(result.error || 'Coordination unavailable');
  return result.space;
}
