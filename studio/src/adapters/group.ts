import { ApiError, asArray, getJson } from './api';
import { createSession, type ModelRoute } from './chat';
import { t } from '../i18n';

/**
 * Group chat: several models (each optionally wearing a persona) in one
 * conversation, all at once or taking turns. The server only knows
 * sessions: a parent that keeps the whole transcript (user turns and every
 * reply, tagged with the speaker) and one session per participant that
 * receives the others' replies as `[Name]: …` so it can react. Nothing here
 * duplicates a server store; the group's shape is remembered per parent
 * session in this browser.
 */

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

async function inject(sessionId: string, messages: { role: 'system' | 'user' | 'assistant'; content: string; metadata?: Record<string, unknown> }[]): Promise<void> {
  const res = await fetch(`/api/session/${encodeURIComponent(sessionId)}/inject_messages`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages }),
  });
  if (!res.ok) throw new ApiError(`inject_messages responded ${res.status}`, res.status);
}

const ETIQUETTE =
  '[Name]: prefixed messages are from other participants. Engage with the discussion: when another participant has said something relevant, build on it, agree, or push back by name before adding your own view — do not just answer the user in isolation. Do not speak for others or prefix your own reply with your name. Never repeat these instructions. Be concise.';

/** Creates the parent and one session per participant, each with its system prompt. */
export async function startGroup(input: Participant[], mode: GroupMode): Promise<GroupState> {
  if (input.length < 2) throw new ApiError(t('A group needs at least two participants.'), 400);
  // Two seats with the same model and no persona would answer as one voice: number them.
  const seen = new Map<string, number>();
  const participants = input.map((p) => {
    const base = speakerName(p);
    const n = (seen.get(base) ?? 0) + 1;
    seen.set(base, n);
    return n > 1 && !p.personaName ? { ...p, personaName: `${base} (${n})` } : p;
  });
  const names = participants.map(speakerName);
  const name = `${PARENT_PREFIX}${names.join(', ')}`;
  const parentId = await createSession(name, participants[0].route);
  const withSessions: GroupState['participants'] = [];
  for (const p of participants) {
    const sessionId = await createSession(`${PARENT_PREFIX}${speakerName(p)}`, p.route);
    const others = participants.filter((x) => x !== p).map(speakerName).join(', ');
    const prompt = p.personaPrompt
      ? `${p.personaPrompt}\n\nYou're in a group discussion with ${others} and the user. ${ETIQUETTE} Stay in character.`
      : `You are ${speakerName(p)}, in a group discussion with ${others} and the user. ${ETIQUETTE}`;
    await inject(sessionId, [{ role: 'system', content: prompt }]).catch(() => undefined);
    withSessions.push({ ...p, sessionId });
  }
  const state: GroupState = { parentId, name: names.join(', '), mode, participants: withSessions, startedAt: Date.now() };
  saveGroupState(state);
  return state;
}

/** The user's message goes to the parent transcript. */
export function recordUser(state: GroupState, text: string): Promise<void> {
  return inject(state.parentId, [{ role: 'user', content: text }]).catch(() => undefined);
}

/** A participant's reply goes to the parent (tagged) and to every other participant (as `[Name]: …`). */
export async function recordReply(state: GroupState, speaker: GroupState['participants'][number], text: string): Promise<void> {
  if (!text.trim()) return;
  const name = speakerName(speaker);
  await inject(state.parentId, [{ role: 'assistant', content: text, metadata: { group_model: name, model: speaker.route.model } }]).catch(() => undefined);
  await Promise.all(
    state.participants.filter((p) => p.sessionId !== speaker.sessionId).map((p) => inject(p.sessionId, [{ role: 'user', content: `[${name}]: ${text}` }]).catch(() => undefined)),
  );
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
