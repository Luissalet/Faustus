import { ApiError, asArray, getJson, responseReason } from './api';
import { askOptionsFrom, type AskUser, type ModelRoute } from './chat';
import { t } from '../i18n';

/** Persistent groups use canonical sessions and the native server conductor. */

export interface Participant {
  route: ModelRoute;
  /** A saved persona (template id) or nothing: the model as itself. */
  personaId: string;
  personaName: string;
  personaPrompt: string;
}

export type GroupMode = 'parallel' | 'round-robin';

export interface GroupState {
  parentId: string;
  name: string;
  mode: GroupMode;
  participants: (Participant & { sessionId: string })[];
  startedAt: number;
  executionMode?: 'chat' | 'agent';
  workspace?: string;
}

export interface GroupPreset {
  name: string;
  participants: { modelId: string; modelDisplay: string; endpointId?: string; characterId?: string; characterName?: string }[];
}

const STATES_KEY = 'fs-group-states';
const PARENT_PREFIX = '[GRP] ';

export function speakerName(p: Pick<Participant, 'route' | 'personaName'>): string {
  return p.personaName || p.route.model.split('/').pop() || p.route.model;
}

export const isGroupSessionName = (name: string) => name.startsWith(PARENT_PREFIX);
export const stripGroupPrefix = (name: string) => name.replace(/^\[GRP\]\s*/, '');

function readStates(): Record<string, GroupState> {
  try {
    return JSON.parse(localStorage.getItem(STATES_KEY) || '{}') as Record<string, GroupState>;
  } catch {
    return {};
  }
}

export function loadGroupState(parentId: string): GroupState | null {
  return readStates()[parentId] ?? null;
}

export function saveGroupState(state: GroupState): void {
  const all = readStates();
  all[state.parentId] = state;
  try {
    localStorage.setItem(STATES_KEY, JSON.stringify(all));
  } catch {
    /* private mode */
  }
}

export function forgetGroupState(parentId: string): void {
  const all = readStates();
  delete all[parentId];
  try {
    localStorage.setItem(STATES_KEY, JSON.stringify(all));
  } catch {
    /* private mode */
  }
}

export function knownGroupParents(): Set<string> {
  return new Set(Object.keys(readStates()));
}

export interface GroupStep {
  id: string; turn_id: string; member_id: string; status: string; text: string; error: string;
  detail: { question?: Record<string, unknown>; last_event?: Record<string, unknown>; run_id?: string; tool_results?: GroupToolResult[] };
}
export interface GroupToolResult { tool?: string; command?: string; output?: string; exit_code?: number; call_id?: string }
export interface GroupSnapshot {
  group: GroupState; busy: boolean;
  turns: { id: string; message: string; status: string }[];
  steps: GroupStep[];
}

async function command<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await fetch(path, { method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return response.json() as Promise<T>;
}

export function groupOptions(signal?: AbortSignal): Promise<{ defaultRoute: ModelRoute | null }> {
  return getJson('/api/groups/config', signal);
}
export async function listGroups(signal?: AbortSignal): Promise<GroupState[]> {
  const result = await getJson<{ groups: GroupState[] }>('/api/groups', signal);
  for (const state of result.groups) saveGroupState(state); // Studio's synchronous navigation cache.
  return result.groups;
}
export function groupSnapshot(id: string, signal?: AbortSignal): Promise<GroupSnapshot> {
  return getJson(`/api/groups/${encodeURIComponent(id)}`, signal);
}
export async function openGroup(id: string, signal?: AbortSignal): Promise<GroupSnapshot> {
  try { return await groupSnapshot(id, signal); }
  catch (error) {
    if (!(error instanceof ApiError) || error.status !== 404) throw error;
    const legacy = loadGroupState(id);
    if (!legacy) throw error;
    // Existing local groups retain their sessions, routes and plain-chat behavior.
    await command('/api/groups/adopt', { ...legacy, executionMode: legacy.executionMode ?? 'chat' });
    return groupSnapshot(id, signal);
  }
}
export async function startGroup(input: Participant[], mode: GroupMode,
  executionMode: 'chat' | 'agent' = 'agent', workspace = ''): Promise<GroupState> {
  const state = await command<GroupState>('/api/groups', { participants: input, mode, executionMode, workspace });
  saveGroupState(state);
  return state;
}
export function sendGroup(id: string, message: string, requestId: string): Promise<unknown> {
  return command(`/api/groups/${encodeURIComponent(id)}/messages`, { message, request_id: requestId });
}
export function stopGroup(id: string): Promise<unknown> {
  return command(`/api/groups/${encodeURIComponent(id)}/stop`);
}
export async function archiveGroup(id: string): Promise<void> {
  await command(`/api/groups/${encodeURIComponent(id)}/archive`);
  forgetGroupState(id);
}
export function resumeGroup(id: string, message: string, answer: Record<string, unknown>): Promise<unknown> {
  return command(`/api/groups/${encodeURIComponent(id)}/resume`, { message, answer });
}
export function questionFrom(step: GroupStep): AskUser | null {
  if (step.status !== 'waiting_user' || !step.detail.question) return null;
  const q = step.detail.question;
  return { question: String(q.question ?? ''), description: typeof q.description === 'string' ? q.description : undefined,
    options: askOptionsFrom(q.options), multi: Boolean(q.multi), kind: q.kind === 'tool_approval' ? 'tool_approval' : 'question',
    approvalId: typeof q.approval_id === 'string' ? q.approval_id : undefined,
    questionId: typeof q.question_id === 'string' ? q.question_id : undefined,
    revision: typeof q.revision === 'number' ? q.revision : undefined };
}

/* ── Saved groups (/api/presets/groups) ── */

export async function listGroupPresets(signal?: AbortSignal): Promise<GroupPreset[]> {
  const raw = await getJson<{ groups?: unknown }>('/api/presets/groups', signal);
  return asArray<Record<string, unknown>>(raw.groups).map((g) => ({
    name: String(g.name ?? ''),
    participants: asArray<Record<string, unknown>>(g.participants).map((p) => ({
      modelId: String(p.modelId ?? ''),
      modelDisplay: String(p.modelDisplay ?? p.modelId ?? ''),
      endpointId: typeof p.endpointId === 'string' ? p.endpointId : undefined,
      characterId: typeof p.characterId === 'string' ? p.characterId : undefined,
      characterName: typeof p.characterName === 'string' ? p.characterName : undefined,
    })),
  }));
}

export async function saveGroupPresets(groups: GroupPreset[]): Promise<void> {
  const res = await fetch('/api/presets/groups', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ groups }) });
  if (!res.ok) throw new ApiError(t('Could not save the group.'), res.status);
}

export function presetFrom(name: string, participants: Participant[]): GroupPreset {
  return {
    name,
    participants: participants.map((p) => ({ modelId: p.route.model, modelDisplay: p.route.model.split('/').pop() ?? p.route.model, endpointId: p.route.endpointId, characterId: p.personaId || undefined, characterName: p.personaName || undefined })),
  };
}
