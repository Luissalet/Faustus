import { agoraGet, agoraPost } from './agora';

/**
 * Threads started from messages, pins and the links of the Agora to the Brain. The hub owns them
 * (`hoard_link/hub/agora_threads.py`); Faustus forwards and says the request is the person's.
 */

export type ThreadState = 'open' | 'closed' | 'archived';
export const THREAD_STATES: ThreadState[] = ['open', 'closed', 'archived'];

export interface AgoraSubthread {
  thread_id: number; title: string; state: ThreadState; channel: string; parent_thread_id: number;
  title_source: 'manual' | 'model' | 'heuristic';
  origin: { message_id: number; to_message_id: number; author: string | null; excerpt: string };
  replies: number; last_at: number | null; last_author: string | null; people: string[];
  /** `null` when nobody is reading as a follower (no reader given). */
  following: boolean | null; unread: number; mentions: number; created_by: string; created: number;
  closed_by: string | null; closed_at: number | null;
}
export interface PinnedMessage { message_id: number; author: string; excerpt: string; pinned_by: string; pinned_at: number; created: number }
export interface BrainCite { page: string; heading: string; label: string }
export interface BrainBacklink {
  kind: 'message' | 'task'; id: number; space_id: string; thread_id: number | null; created: number; heading: string;
  author?: string; channel?: string; excerpt?: string; title?: string; status?: string; owner?: string | null;
}

export const suggestThreadTitle = (space: string, messageId: number, toMessageId?: number) =>
  agoraPost<{ title: string; source: 'model' | 'heuristic'; model: string; messages: number }>('thread_title', { space_id: space, message_id: messageId, to_message_id: toMessageId });

export const startThread = (space: string, input: { messageId: number; toMessageId?: number; title?: string; body?: string }) =>
  agoraPost<{ existing: boolean; title_source: string; thread: AgoraSubthread }>('thread_from', {
    space_id: space, message_id: input.messageId, to_message_id: input.toMessageId, title: input.title, body: input.body });

export const setThread = (space: string, threadId: number, changes: { title?: string; state?: ThreadState }) =>
  agoraPost<{ changed: Record<string, string>; thread: AgoraSubthread }>('thread_set', { space_id: space, thread_id: threadId, ...changes });

export const followThread = (space: string, threadId: number, follow: boolean) =>
  agoraPost<{ following: boolean; thread: AgoraSubthread }>('thread_follow', { space_id: space, thread_id: threadId, follow });

export const listThreads = (space: string, opts: { scope?: 'following' | 'all'; state?: ThreadState | 'all'; unread?: boolean } = {}, signal?: AbortSignal) =>
  agoraGet<{ threads: AgoraSubthread[]; total: number; unread: number; mentions: number; scope: string; state: string }>(
    'thread_list', { space_id: space, scope: opts.scope ?? 'following', state: opts.state ?? 'open', unread: opts.unread }, signal);

export const pinMessage = (space: string, messageId: number, pinned: boolean) =>
  agoraPost<{ pinned: boolean; pins: PinnedMessage[] }>('message_pin', { space_id: space, message_id: messageId, pinned });

/** Where the Agora cites a Brain page (messages and tasks of every server). */
export const brainBacklinks = (page: string, aliases: string[] = [], signal?: AbortSignal) =>
  agoraGet<{ backlinks: BrainBacklink[]; total: number }>('brain_backlinks', { page, aliases: aliases.join('|') || undefined }, signal);

/** What the hub adds to a message of a page (`thread`, `thread_span`, `pinned`, `brain`) and to the page itself. */
export interface MessageThreadInfo { thread?: AgoraSubthread; thread_span?: number; pinned?: boolean; brain?: BrainCite[] }
export interface PageThreadInfo { pins?: PinnedMessage[]; subthread?: AgoraSubthread }
