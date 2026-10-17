import type { HistoryMessage } from '../../adapters/chat';
import { metricsFrom } from '../../adapters/chat';
import { attachmentsFromMetadata } from '../../adapters/composer';
import { isPassText, isPeerTurn, speakerName, type GroupSnapshot, type GroupStep, type Participant } from '../../adapters/group';
import { blankTurn, restoreFromMetadata, type Turn } from '../studio/model';
import { t } from '../../i18n';

/**
 * A room's transcript in Studio's own turn model, so the chat is drawn by
 * the same Transcript as any conversation: the person's words on the right,
 * each member's answer with its tool rail, thinking and timings.
 *
 * Finished answers come from the room's canonical session (the server copies
 * each member's full answer record there). Answers still being written come
 * from the live run of that member (`live`), or from the room's step record
 * while no live stream is attached yet.
 */

type Member = Participant & { sessionId: string };

/** @handles of every member: full name plus a short, unique prefix (as the server). */
export function memberHandles(members: Member[]): Map<string, string[]> {
  const raw = new Map<string, string[]>();
  for (const member of members) {
    const name = speakerName(member).trim();
    const out = [name.toLowerCase()];
    const first = (name.split(/[\s\-_.:/()]+/)[0] || '').toLowerCase();
    const alpha = (/^[A-Za-z]+/.exec(name)?.[0] || '').toLowerCase();
    for (const candidate of [first, alpha]) if (candidate.length >= 3 && !out.includes(candidate)) out.push(candidate);
    raw.set(member.sessionId, out);
  }
  const counts = new Map<string, number>();
  for (const values of raw.values()) for (const value of new Set(values)) counts.set(value, (counts.get(value) ?? 0) + 1);
  const result = new Map<string, string[]>();
  for (const [sid, values] of raw) result.set(sid, [values[0], ...values.slice(1).filter((v) => counts.get(v) === 1)]);
  return result;
}

export function shortHandle(member: Member, members: Member[]): string {
  const values = memberHandles(members).get(member.sessionId) ?? [speakerName(member)];
  return values[values.length - 1];
}

const escape = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Members the person addressed with @handle: only they answer first. */
export function mentionsIn(text: string, members: Member[]): string[] {
  const found: string[] = [];
  for (const [sid, values] of memberHandles(members)) {
    for (const value of [...values].sort((a, b) => b.length - a.length)) {
      if (new RegExp('(?<![\\w@])@' + escape(value) + '(?![\\w-]*\\w)', 'i').test(text)) {
        found.push(sid);
        break;
      }
    }
  }
  return found;
}

/** The `@partial` being typed at the caret, if any. */
export function mentionQuery(text: string, caret: number): { start: number; query: string } | null {
  const before = text.slice(0, caret);
  const match = /(?:^|[\s(])@([\w.-]*)$/.exec(before);
  if (!match) return null;
  return { start: caret - match[1].length - 1, query: match[1].toLowerCase() };
}

function historyTurn(m: HistoryMessage): Turn | null {
  const meta = m.metadata ?? {};
  if (m.role === 'user') {
    const turn = blankTurn('user', m.content);
    turn.attachments = attachmentsFromMetadata(meta);
    turn.id = 'room-' + String(meta.group_turn_id ?? meta.group_answer_id ?? m.index);
    turn.streaming = false;
    return turn;
  }
  if (m.role !== 'assistant') return null;
  const passed = meta.group_pass === true || (typeof meta.group_step_id === 'string' && !meta.group_error && isPassText(m.content));
  let turn = blankTurn('assistant', passed ? t('_Nothing to add._') : m.content);
  turn.id = String(meta.group_step_id ?? 'room-' + m.index);
  turn.streaming = false;
  turn.live = undefined;
  turn.metrics = metricsFrom(meta);
  // Older rooms kept only the raw tool results; they draw the same rail.
  const tools = Array.isArray(meta.tool_events) ? meta.tool_events : Array.isArray(meta.group_tool_results) ? meta.group_tool_results : [];
  turn = restoreFromMetadata(turn, { ...meta, tool_events: tools });
  if (!turn.speaker) turn.speaker = String(meta.group_model ?? meta.model ?? '') || undefined;
  if (typeof meta.group_error === 'string' && meta.group_error) turn.error = meta.group_error;
  return turn;
}

function stepTurn(step: GroupStep, member: Member | undefined): Turn {
  const displayed = step.status === 'running' && typeof step.detail.display_text === 'string' ? step.detail.display_text : step.text;
  let turn = blankTurn('assistant', displayed);
  turn.id = step.id;
  turn = restoreFromMetadata(turn, { tool_events: step.detail.tool_results ?? [] });
  turn.speaker = member ? speakerName(member) : undefined;
  turn.streaming = step.status === 'running';
  if (!turn.streaming) turn.live = undefined;
  if (step.error) turn.error = step.error;
  return turn;
}

export interface RoomView {
  turns: Turn[];
  /** Member session id → what that member is doing now. */
  activity: Record<string, { state: 'speaking' | 'next' | 'asks' | 'idle'; stepId?: string }>;
}

export function roomView(history: HistoryMessage[], snapshot: GroupSnapshot | null, live: Record<string, Turn>): RoomView {
  const turns: Turn[] = [];
  const seenTurns = new Set<string>();
  const seenSteps = new Set<string>();
  for (const message of history) {
    const meta = message.metadata ?? {};
    if (typeof meta.group_turn_id === 'string' && message.role === 'user') seenTurns.add(meta.group_turn_id);
    if (typeof meta.group_step_id === 'string') seenSteps.add(meta.group_step_id);
    const turn = historyTurn(message);
    if (turn) turns.push(turn);
  }
  const members = snapshot?.group.participants ?? [];
  const activity: RoomView['activity'] = Object.fromEntries(members.map((m) => [m.sessionId, { state: 'idle' as const }]));
  for (const turn of snapshot?.turns ?? []) {
    const peer = isPeerTurn(turn);
    if (!peer && !seenTurns.has(turn.id) && turn.status !== 'stopped') {
      const pending = blankTurn('user', turn.message);
      pending.id = 'room-' + turn.id;
      pending.streaming = false;
      if (turn.status === 'queued') pending.note = t('Queued: the room answers it after the current round.');
      turns.push(pending);
    }
    const roster = turn.participants ?? members;
    for (const step of snapshot?.steps.filter((s) => s.turn_id === turn.id) ?? []) {
      if (step.status === 'queued' && activity[step.member_id]?.state === 'idle') activity[step.member_id] = { state: 'next', stepId: step.id };
      if (step.status === 'running') activity[step.member_id] = { state: 'speaking', stepId: step.id };
      if (step.status === 'waiting_user') activity[step.member_id] = { state: 'asks', stepId: step.id };
      if (seenSteps.has(step.id) || step.status === 'queued' || (step.status === 'stopped' && !step.text)) continue;
      const member = roster.find((p) => p.sessionId === step.member_id) as Member | undefined;
      const streamed = step.status === 'running' ? live[step.id] : undefined;
      const saved = stepTurn(step, member);
      turns.push(streamed ? { ...streamed, text: streamed.text || saved.text, speaker: saved.speaker ?? streamed.speaker } : saved);
    }
  }
  return { turns, activity };
}
