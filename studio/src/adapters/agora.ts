import { CLIENT_API_VERSION, CLIENT_VERSION_HEADER } from './api';
import { t } from '../i18n';

/**
 * The Agora as servers and channels. The family hub owns the data; Faustus
 * forwards (`routes/agora_routes.py`) and says the request comes from the
 * person signed in here, so what is written from Studio is theirs.
 */

export type BoardColumn = 'open' | 'in_progress' | 'review' | 'done';
export const BOARD_COLUMNS: BoardColumn[] = ['open', 'in_progress', 'review', 'done'];
export type Presence = 'person' | 'working' | 'idle' | 'waiting' | 'away' | 'offline' | 'no_quota';

export interface ServerCounts { open: number; active: number; review: number; blocked: number; waiting: number }
export interface AgoraServer {
  id: string; title: string; description: string; project_id: string; workspace: string; revision: number;
  members: number; online: number; counts: ServerCounts; last_activity: number | null; unread: number; mentions: number;
  icon: string; color: string; folders: string[]; position: number | null; hidden: boolean; icon_app?: string | null;
}
export interface AgoraChannel {
  slug: string; title: string; topic: string; category: string; position: number; thread_id: number | null;
  builtin: boolean; archived: boolean; messages: number; last_at: number | null; last_author: string | null;
  last_text: string; unread: number; mentions: number;
}
export interface AgoraTaskRow {
  id: number; title: string; status: string; owner: string | null; reviewer: string | null; kind: string;
  priority: number; repo: string | null; thread_id: number | null; created: number; updated: number;
  submitted_at: number | null; closed_at: number | null; review_state: string | null; created_by: string;
  unread: number; mentions: number; waiting_on: number[]; column: BoardColumn;
}
export interface AgoraThreadRow {
  id: number; title: string; kind: string; status: string; created_by: string; created: number; updated: number;
  messages: number; last_author: string | null; unread: number; mentions: number;
}
export interface AgoraWaiting { id: number; title: string; kind: string; task_id: number | null; updated: number; question: string | null; escalated_by: string | null }
export interface MemberQuota { state: string; remaining_percent: number | null; reset_at: string | null; note: string | null; pool_label: string | null }
export interface AgoraMember {
  id: string; name: string; kind: string; role: string; presence: Presence; state: string; doing: string; note: string;
  last_seen: number | null; locks: number; tasks: { id: number; title: string; status: string }[]; quota: MemberQuota;
  observed?: { state?: string; tool?: { name?: string; input?: string; since?: number } | null; title?: string; engine?: string; last_activity?: number; questions?: unknown[] } | null;
}
export interface QuotaWindow { window_id: string; label?: string; remaining_percent: number | null; reset_at?: string | number | null; stale?: boolean }
export interface QuotaPool { pool_id: string; pool_label?: string; reported_at?: string; stale?: boolean; windows?: QuotaWindow[] }
export interface QuotaProvider { provider_id: string; pools?: QuotaPool[] }
export interface AgoraSpaceInfo {
  id: string; title: string; description: string; project_id: string; workspace: string; revision: number; members: number;
  created_by: string; meta: { icon: string; color: string; folders: string[]; position: number | null; hidden: boolean };
}
export interface AgoraGuild {
  ok: boolean; version: string; viewer: string | null; space: AgoraSpaceInfo; channels: AgoraChannel[];
  views: Record<string, { count?: number; counts?: Record<string, number> }>;
  threads: AgoraThreadRow[]; waiting: AgoraWaiting[]; tasks: Record<BoardColumn, AgoraTaskRow[]>;
  task_counts: Record<string, number>; column_counts: Record<BoardColumn, number>; members: AgoraMember[]; members_older?: number;
  providers: QuotaProvider[];
}
export interface AgoraMessage {
  id: number; thread_id: number; author: string; kind: string; body: string; mentions: string[]; created: number;
  body_truncated?: boolean;
}
export interface AgoraTask {
  id: number; title: string; body: string; status: string; owner: string | null; reviewer: string | null; kind: string;
  priority: number; repo: string | null; thread_id: number; commits: string[]; branch: string | null; result: string | null;
  submission_revision: number; review_state: string | null; etag: string; waiting_on: number[]; created_by: string;
  blocked_by: { id: number; title: string; status: string; owner: string | null }[]; acceptance?: string; labels?: string[];
}
export interface AgoraThreadInfo { id: number; title: string; kind: string; status: string; task_id: number | null; created_by: string; resolution?: string | null; resolved_by?: string | null }
export interface AgoraPage {
  ok: boolean; channel: string | null; thread: AgoraThreadInfo | null; messages: AgoraMessage[]; seen_upto: number;
  task?: AgoraTask | null; pagination: { limit: number; has_more: boolean; next_before_id: number | null };
}
export interface FeedItem {
  id: number; title: string; status: string; owner: string | null; reviewer: string | null; review_state: string | null;
  kind: string; repo: string | null; branch: string | null; commits: string[]; thread_id: number | null;
  submitted_at: number | null; closed_at: number | null; at: number; summary: string; summary_truncated: boolean;
  column: BoardColumn; submission_revision: number;
}
export interface ActivityItem {
  id: number; thread_id: number; author: string; kind: string; body: string; mentions: string[]; created: number;
  thread_title: string; thread_kind: string; task_id: number | null;
}
export interface Decision { id: number; title: string; kind: string; resolution: string; resolved_by: string; updated: number }
export interface AgoraLock { resource: string; owner: string; task_id: number | null; note: string | null; expires: number }

type Params = Record<string, string | number | boolean | undefined | null>;

function query(params?: Params): string {
  const parts = Object.entries(params ?? {}).filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(String(v)));
  return parts.length ? '?' + parts.join('&') : '';
}

async function unwrap<T>(response: Response): Promise<T> {
  let data: { ok?: boolean; error?: unknown; detail?: unknown; code?: unknown } | null = null;
  try { data = await response.json(); } catch { /* not JSON */ }
  if (data && data.ok === false && data.code === 'hub_unreachable') {
    throw new Error(t('The Hoard Hub is not answering. The Agora lives there, on the PC, and comes back when the Hub does.'));
  }
  if (!response.ok || !data || data.ok === false) {
    const reason = data && typeof data.error === 'string' ? data.error : data && typeof data.detail === 'string' ? data.detail : `Agora responded ${response.status}`;
    throw new Error(reason);
  }
  return data as T;
}

export async function agoraGet<T>(path: string, params?: Params, signal?: AbortSignal): Promise<T> {
  return unwrap<T>(await fetch('/api/agora/x/' + path + query(params), {
    signal, credentials: 'same-origin', headers: { Accept: 'application/json', [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION } }));
}

export async function agoraPost<T>(op: string, body: Record<string, unknown>): Promise<T> {
  return unwrap<T>(await fetch('/api/agora/x/' + op, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION }, body: JSON.stringify(body) }));
}

export const getServers = (signal?: AbortSignal) =>
  agoraGet<{ servers: AgoraServer[]; version: string; default_space_id: string }>('servers', undefined, signal);
export const getGuild = (space: string, signal?: AbortSignal) => agoraGet<AgoraGuild>('guild', { space_id: space, done_limit: 30 }, signal);

export async function pulse(since: string, signal?: AbortSignal): Promise<{ version: string; changed: boolean }> {
  return unwrap(await fetch('/api/agora/pulse' + query({ since, wait_s: 20 }), {
    signal, credentials: 'same-origin', headers: { Accept: 'application/json', [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION } }));
}

/** A conversation is a channel of the space or any thread (a task's, a debate…). */
export type Target = { channel: string } | { threadId: number };
const targetArgs = (target: Target) => 'channel' in target ? { channel: target.channel } : { thread_id: target.threadId };

export const readTarget = (space: string, target: Target, opts: { limit?: number; before?: number; peek?: boolean } = {}) =>
  agoraPost<AgoraPage>('channel_read', { space_id: space, ...targetArgs(target), limit: opts.limit ?? 60,
    ...(opts.before ? { before_id: opts.before } : {}), ...(opts.peek ? { peek: true } : {}) });

export const say = (space: string, target: Target, body: string, kind = 'comment', mentions: string[] = []) =>
  agoraPost<{ message_id: number; mentions: string[]; channel: string | null; thread: AgoraThreadInfo }>(
    'channel_post', { space_id: space, ...targetArgs(target), body, kind, mentions });

export const createChannel = (space: string, input: { title: string; topic?: string; category?: string }) =>
  agoraPost<{ channel: AgoraChannel }>('channel_create', { space_id: space, ...input });
export const updateChannel = (space: string, channel: string, changes: Record<string, unknown>) =>
  agoraPost<{ channel: AgoraChannel }>('channel_update', { space_id: space, channel, ...changes });

/** Whether the hub (and with it the agents) is on another machine than this Faustus. Folders are then chosen there. */
export async function hubIsRemote(signal?: AbortSignal): Promise<boolean> {
  try {
    const res = await fetch('/api/agora/where', { signal, credentials: 'same-origin', headers: { Accept: 'application/json' } });
    return res.ok ? Boolean((await res.json() as { remote?: boolean }).remote) : false;
  } catch { return false; }
}
export interface HubFolders { path: string; parent: string | null; home: string; sep: string; folders: { name: string; path: string }[]; truncated?: boolean }
/** The folders inside `path` on the hub's machine (its drives when empty). */
export const hubFolders = (path: string, signal?: AbortSignal) => agoraGet<HubFolders>('folders', { path }, signal);
/** A new space (a server), created as the person. */
export const createServerSpace = (title: string, extra: { project_id?: string; workspace?: string; description?: string } = {}) =>
  agoraPost<{ space: AgoraSpaceInfo }>('space_create', { title, ...Object.fromEntries(Object.entries(extra).filter(([, v]) => v)) }).then((r) => r.space);

export const markSpaceSeen = (space: string) => agoraPost<{ seen_upto: number }>('seen', { space_id: space });
export const setSpaceMeta = (space: string, meta: Record<string, unknown>) =>
  agoraPost<{ meta: AgoraSpaceInfo['meta'] }>('space_meta_set', { space_id: space, ...meta });
export const updateSpace = (space: string, revision: number, changes: Record<string, unknown>) =>
  agoraPost<{ space: AgoraSpaceInfo }>('space_update', { space_id: space, expected_revision: revision, ...changes });

/** `before` is the `next_before` the previous page gave, passed back as it came. */
export const getFeed = <T = FeedItem>(space: string, kind: 'deliveries' | 'reviews' | 'activity', before?: string | number | null, signal?: AbortSignal) =>
  agoraGet<{ items: T[]; next_before: string | number | null }>('feed', { space_id: space, kind, limit: 40, before: before ?? undefined }, signal);
export const getDecisions = (space: string, signal?: AbortSignal) =>
  agoraGet<{ decisions: Decision[] }>('decisions', { space_id: space, limit: 100 }, signal);
export const getLocks = (signal?: AbortSignal) => agoraGet<{ locks: AgoraLock[] }>('locks', undefined, signal);
export const getTask = (space: string, id: number, signal?: AbortSignal) =>
  agoraGet<{ task: AgoraTask }>('tasks/' + id, { space_id: space }, signal);

export const addTask = (space: string, input: { title: string; body?: string; kind?: string; priority?: number; owner?: string; repo?: string }) =>
  agoraPost<{ task: AgoraTask }>('task_add', { space_id: space, ...input });
export const editTask = (space: string, task: AgoraTask, changes: Record<string, unknown>) =>
  agoraPost<{ task: AgoraTask }>('task_edit', { space_id: space, task_id: task.id, expected_etag: task.etag, ...changes });
export const reviewTask = (space: string, task: AgoraTask, verdict: 'approve' | 'changes', body: string) =>
  agoraPost<{ task: AgoraTask }>('task_review', { space_id: space, task_id: task.id, verdict, body,
    expected_submission_revision: task.submission_revision });
export const dropTask = (space: string, id: number, reason: string) =>
  agoraPost<{ task: AgoraTask }>('task_release', { space_id: space, task_id: id, drop: true, reason });

export const openThread = (space: string, input: { title: string; body: string; kind: string; mentions?: string[] }) =>
  agoraPost<{ thread: AgoraThreadInfo }>('thread_open', { space_id: space, ...input });
export const resolveThread = (space: string, threadId: number, resolution: string) =>
  agoraPost<{ thread: AgoraThreadInfo }>('resolve', { space_id: space, thread_id: threadId, resolution });
export const reopenThread = (space: string, threadId: number, reason: string) =>
  agoraPost<{ thread: AgoraThreadInfo }>('reopen', { space_id: space, thread_id: threadId, reason });

export interface SearchHit {
  q?: string;
  tasks: { id: number; title: string; status: string; owner: string | null; thread_id: number | null; space_id: string; column: BoardColumn }[];
  threads: { id: number; title: string; kind: string; status: string; space_id: string }[];
  messages: { id: number; thread_id: number; author: string; kind: string; created: number; thread_title: string; thread_kind: string; task_id: number | null; space_id: string; snippet: string }[];
}
export const searchAgora = (space: string, q: string, everywhere: boolean, signal?: AbortSignal) =>
  agoraGet<SearchHit>('search', { space_id: space, q, limit: 40, ...(everywhere ? { all_spaces: 1 } : {}) }, signal);

export interface InboxMessage { id: number; thread_id: number; author: string; kind: string; body: string; created: number; thread_title: string; thread_kind: string; task_id: number | null; space_id: string }
export interface PersonInbox {
  escalated: { id: number; title: string; kind: string; space_id: string; updated: number; question: string | null; escalated_by: string | null }[];
  pending: InboxMessage[];
  reviews: { id: number; title: string; owner: string | null; thread_id: number | null; space_id: string; submitted_at: number | null }[];
}
/** What waits for the person in every server: questions, mentions and submissions without a reviewer. */
export const getInbox = (signal?: AbortSignal) =>
  agoraGet<PersonInbox>('inbox', { agent: 'luis', all_spaces: 1, peek: 1, mine_only: 1, limit: 40 }, signal);

/** `s` names the size served, so a browser never shows an older, heavier copy it kept. */
export const iconUrl = (app: string) => '/api/agora/icon/' + encodeURIComponent(app) + '?s=128';
