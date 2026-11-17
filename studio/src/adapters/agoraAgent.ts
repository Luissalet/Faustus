import { agoraGet, agoraPost } from './agora';

/**
 * One agent's page in the Agora: its profile, the direct messages to it and the
 * changes to its instructions. The hub owns all of it (`agora_profiles.py`);
 * these calls go through the generic Agora proxy, which writes as the person.
 */

export interface AgentHeader {
  id: string; name: string; initials: string; kind: string; provider: string | null; engine: string;
  model: string | null; effort: string | null; model_source: string | null; rank: string; presence: string; state: string;
  doing: string; note: string; last_seen: number | null; first_seen: number | null; since: number | null;
  since_source: 'session' | 'last_seen'; color: string | null; locks: number; in_server: boolean;
  tasks_in_hand: { id: number; title: string; status: string }[];
  quota: { state?: string; remaining_percent?: number | null; reset_at?: string | null; note?: string | null };
}
export interface AgentTimelineItem {
  at: number | null; type: 'message' | 'task' | 'delivery'; text: string; where?: string; message_id?: number;
  task_id?: number | null; commits?: string[]; status?: string; role?: string;
}
export interface AgentMemoryItem { id: number | null; kind: 'learned' | 'decision' | 'correction' | 'note'; text: string; source: string; by: string; at: number; space_id: string }
export interface AgentRoutine { name: string; schedule: string; note: string; enabled: boolean; updated_by?: string; updated?: number }
export interface AgentUsageDay { day: string; messages: number; tasks_closed: number; run_minutes: number }
export interface InstructionProposal {
  id: number; agent: string; space_id: string; body: string; base_body: string; reason: string; trigger: string; source: string;
  status: 'pending' | 'accepted' | 'discarded'; created: number; decided_by: string | null; decided_at: number | null;
  decision_note: string; stale: boolean; diff: string;
}
export interface AgentProfile {
  ok: boolean; profile: AgentHeader;
  activity: { messages: unknown[]; tasks: unknown[]; deliveries: { task_id: number; title: string; at: number; commits: string[] }[]; commits: string[]; timeline: AgentTimelineItem[] };
  instructions: { body: string; updated_by: string | null; updated: number | null };
  memory: AgentMemoryItem[]; routines: AgentRoutine[];
  usage: { days: AgentUsageDay[]; tokens: number | null; note: string };
  proposals: InstructionProposal[]; proposals_pending: number; dm: { channel: string; thread_id: number | null } | null;
}
export interface DmRow {
  slug: string; title: string; thread_id: number | null; with_agent: string; participants: string[];
  messages: number; last_at: number | null; last_author: string | null; last_text: string; unread: number; mentions: number;
}
export interface TellResult {
  ok: boolean; message_id: number; channel: string; thread_id: number; to: string; correction: boolean;
  delivery: { status: string; detail: string; wake?: { status?: string; detail?: string } };
}

export const getAgentProfile = (space: string, id: string, signal?: AbortSignal) =>
  agoraGet<AgentProfile>('agent_profile', { space_id: space, id, limit: 40 }, signal);
export const listDms = (space: string, signal?: AbortSignal) =>
  agoraGet<{ ok: boolean; dms: DmRow[] }>('dm_list', { space_id: space }, signal);
export const getProposals = (space: string, id?: string, status?: string) =>
  agoraGet<{ ok: boolean; proposals: InstructionProposal[]; pending: number }>('agent_instruction_proposals', { space_id: space, id, status });
export const tellAgent = (space: string, to: string, text: string, correction = false) =>
  agoraPost<TellResult>('agent_tell', { space_id: space, to, text, correction });
export const setInstructions = (space: string, id: string, body: string) =>
  agoraPost<{ ok: boolean }>('agent_instructions_set', { space_id: space, id, body });
export const decideProposal = (space: string, proposalId: number, decision: 'accept' | 'discard', opts: { body?: string; note?: string } = {}) =>
  agoraPost<{ ok: boolean; proposal: InstructionProposal }>('agent_instruction_decide', { space_id: space, proposal_id: proposalId, decision, ...opts });
export const addMemory = (space: string, id: string, text: string, kind: AgentMemoryItem['kind']) =>
  agoraPost<{ ok: boolean }>('agent_memory_add', { space_id: space, id, text, kind });
export const removeMemory = (space: string, memoryId: number) =>
  agoraPost<{ ok: boolean }>('agent_memory_remove', { space_id: space, memory_id: memoryId });
