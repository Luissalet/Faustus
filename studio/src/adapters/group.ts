import { ApiError, asArray, getJson, responseReason } from './api';
import { askOptionsFrom, type AskUser, type ModelRoute } from './chat';
import { t } from '../i18n';

/** Persistent groups use canonical sessions and the native server conductor. */

export interface Participant {
  sessionId?: string;
  route: ModelRoute;
  /** A saved persona (template id) or nothing: the model as itself. */
  personaId: string;
  personaName: string;
  personaPrompt: string;
  autonomyPreset?: 'supervised' | 'bounded_autonomous' | 'read_only';
  inputTokenBudget?: number;
}

export type GroupMode = 'parallel' | 'round-robin';

export interface GroupState {
  revision?: number;
  parentId: string;
  name: string;
  mode: GroupMode;
  participants: (Participant & { sessionId: string })[];
  startedAt: number;
  executionMode?: 'chat' | 'agent';
  workspace?: string;
  space_id?: string;
  /** The project the room works in (its members are chats of that project). */
  projectId?: string;
  /** Follow-up rounds in which the members answer each other (0 = none). */
  discussionRounds?: number;
}

/** Same default as the server (`src/group_discussion.py`). */
export const DEFAULT_DISCUSSION_ROUNDS = 3;
/** Follow-up rounds are queued turns whose request id starts with this. */
export const isPeerTurn = (turn: { request_id?: string }) => String(turn.request_id ?? '').startsWith('peer:');
export const PASS_RE = /^\W*\[\s*pass\s*\]|^\W*pass\W*$/i;
export const isPassText = (text: string) => !text.trim() || PASS_RE.test(text.trim());

export interface GroupPreset {
  name: string;
  participants: { modelId: string; modelDisplay: string; endpointId?: string; characterId?: string; characterName?: string;
    personaPrompt?: string; autonomyPreset?: Participant['autonomyPreset']; inputTokenBudget?: number }[];
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
export interface GroupToolResult { tool?: string; command?: string; output?: string; exit_code?: number; call_id?: string; result_status?: string; uncertainty?: unknown; [key: string]: unknown }
export interface GroupSnapshot {
  group: GroupState; busy: boolean;
  turns: { id: string; message: string; status: string; request_id?: string; created?: number; participants?: (Participant & {sessionId: string})[] }[];
  steps: GroupStep[];
}

async function command<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await fetch(path, { method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return response.json() as Promise<T>;
}

export async function updateGroup(state: GroupState, changes: Partial<GroupState>): Promise<GroupState> {
  const response = await fetch('/api/groups/' + encodeURIComponent(state.parentId), {method: 'PATCH', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...changes, expected_revision: state.revision})});
  if (!response.ok) throw new ApiError(await responseReason(response, 'Group'), response.status);
  return response.json() as Promise<GroupState>;
}

export function groupOptions(signal?: AbortSignal): Promise<{ defaultRoute: ModelRoute | null }> {
  return getJson('/api/groups/config', signal);
}
export async function listGroups(signal?: AbortSignal, spaceId?: string): Promise<GroupState[]> {
  const result = await getJson<{ groups: GroupState[] }>('/api/groups' + (spaceId ? '?space_id=' + encodeURIComponent(spaceId) : ''), signal);
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
  executionMode: 'chat' | 'agent' = 'agent', workspace = '', spaceId = 'ecosystem', name = '',
  extra: { projectId?: string; discussionRounds?: number } = {}): Promise<GroupState> {
  const state = await command<GroupState>('/api/groups', { participants: input, mode, executionMode, workspace, space_id: spaceId, name,
    project_id: extra.projectId ?? '', discussionRounds: extra.discussionRounds ?? DEFAULT_DISCUSSION_ROUNDS });
  saveGroupState(state);
  return state;
}

/* ── The Agora task board of a space (the Hub's, trimmed for a kanban) ── */

export interface BoardTask {
  id: number; title: string; status: string; owner: string | null; reviewer: string | null; kind: string;
  priority: number | null; repo: string; body: string; activity: string | null; review_state: string | null;
  updated: number | null; closed_at: number | null; created_by: string | null;
}
export type BoardColumn = 'open' | 'in_progress' | 'review' | 'done';
export interface AgoraBoard {
  space_id: string; columns: Record<BoardColumn, BoardTask[]>; counts: Record<string, number>; done_total: number;
  agents: { id: string; state: string | null; doing: string | null; stale: boolean | null }[];
}
export async function agoraBoard(spaceId: string, signal?: AbortSignal): Promise<AgoraBoard> {
  const result = await getJson<{ ok: boolean; error?: string } & Partial<AgoraBoard>>(
    '/api/hoard/agora/board?space_id=' + encodeURIComponent(spaceId), signal);
  if (!result.ok || !result.columns) throw new Error(result.error || t('The Agora board is unavailable.'));
  return result as AgoraBoard;
}
export function sendGroup(id: string, message: string, requestId: string, mentions: string[] = []): Promise<{turn_id: string}> {
  return command(`/api/groups/${encodeURIComponent(id)}/messages`, { message, request_id: requestId, mentions });
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
      personaPrompt: typeof p.personaPrompt === 'string' ? p.personaPrompt : undefined,
      autonomyPreset: ['supervised', 'bounded_autonomous', 'read_only'].includes(String(p.autonomyPreset)) ? p.autonomyPreset as Participant['autonomyPreset'] : undefined,
      inputTokenBudget: typeof p.inputTokenBudget === 'number' ? p.inputTokenBudget : undefined,
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
    participants: participants.map((p) => ({ modelId: p.route.model, modelDisplay: p.route.model.split('/').pop() ?? p.route.model,
      endpointId: p.route.endpointId, characterId: p.personaId || undefined, characterName: p.personaName || undefined,
      personaPrompt: p.personaPrompt || undefined, autonomyPreset: p.autonomyPreset, inputTokenBudget: p.inputTokenBudget })),
  };
}
