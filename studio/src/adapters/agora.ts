import { CLIENT_API_VERSION, CLIENT_VERSION_HEADER } from './api';
import { t } from '../i18n';

/**
 * The Agora as servers and channels. The family hub owns the data; Faustus
 * forwards (`routes/agora_routes.py`) and says the request comes from the
 * person signed in here, so what is written from Studio is theirs.
 */

export type BoardColumn = 'open' | 'in_progress' | 'review' | 'done';
export const BOARD_COLUMNS: BoardColumn[] = ['open', 'in_progress', 'review', 'done'];
export type Presence = 'person' | 'working' | 'idle' | 'waiting' | 'away' | 'offline' | 'no_quota' | 'invited';

export interface ServerCounts { open: number; active: number; review: number; blocked: number; waiting: number }
export interface AgoraServer {
  id: string; title: string; description: string; project_id: string; workspace: string; revision: number;
  members: number; online: number; counts: ServerCounts; last_activity: number | null; unread: number; mentions: number;
  icon: string; color: string; folders: string[]; position: number | null; hidden: boolean; icon_app?: string | null;
  /** The person silenced it: nothing that happens in it is announced outside the Agora. */
  muted?: boolean;
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
  /** Its rank on this server (`role` is its provider): director, lead, agent or support; `person` for the person. */
  rank?: string; rank_level?: number;
  /** Self-reported by its session's heartbeat, never a verified console setting. */
  declared_model?: string | null; declared_effort?: string | null; declared_at?: string | null;
  last_seen: number | null; locks: number; tasks: { id: number; title: string; status: string }[]; quota: MemberQuota;
  observed?: { state?: string; tool?: { name?: string; input?: string; since?: number } | null; title?: string; engine?: string; last_activity?: number; questions?: unknown[] } | null;
}
export interface QuotaWindow { window_id: string; label?: string; remaining_percent: number | null; reset_at?: string | number | null; stale?: boolean }
export interface QuotaPool { pool_id: string; pool_label?: string; reported_at?: string; stale?: boolean; windows?: QuotaWindow[] }
export interface QuotaProvider { provider_id: string; pools?: QuotaPool[] }
export interface AgoraSpaceInfo {
  id: string; title: string; description: string; project_id: string; workspace: string; revision: number; members: number;
  created_by: string; meta: { icon: string; color: string; folders: string[]; position: number | null; hidden: boolean; muted?: boolean };
}
export interface AgoraGuild {
  ok: boolean; version: string; viewer: string | null; space: AgoraSpaceInfo; channels: AgoraChannel[];
  views: Record<string, { count?: number; counts?: Record<string, number> }>;
  threads: AgoraThreadRow[]; waiting: AgoraWaiting[]; tasks: Record<BoardColumn, AgoraTaskRow[]>;
  task_counts: Record<string, number>; column_counts: Record<BoardColumn, number>; members: AgoraMember[]; members_older?: number;
  providers: QuotaProvider[];
}
export interface MessageReaction { emoji: string; count: number; agents: string[]; mine: boolean }
export interface MessageFile { id: string; name: string; type: string; size: number; inline: boolean }
/** The message another one quotes; `gone` when it is no longer there. */
export interface MessageQuote { id: number; author: string | null; excerpt: string; gone?: boolean }
export interface AgoraMessage {
  id: number; thread_id: number; author: string; kind: string; body: string; mentions: string[]; created: number;
  body_truncated?: boolean; reactions?: MessageReaction[]; reply?: MessageQuote | null; files?: MessageFile[];
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

/** `carry`: the message it quotes and the files it takes along (sent first with `sendFile`). */
export const say = (space: string, target: Target, body: string, kind = 'comment', mentions: string[] = [],
  carry: { replyTo?: number; files?: string[] } = {}) =>
  agoraPost<{ message_id: number; mentions: string[]; channel: string | null; thread: AgoraThreadInfo }>(
    'channel_post', { space_id: space, ...targetArgs(target), body, kind, mentions,
      ...(carry.replyTo ? { reply_to: carry.replyTo } : {}), ...(carry.files?.length ? { files: carry.files } : {}) });

/** Put a reaction on a message or take it back (without `on`, it toggles). */
export const react = (space: string, messageId: number, emoji: string, on?: boolean) =>
  agoraPost<{ message_id: number; on: boolean; reactions: MessageReaction[] }>(
    'react', { space_id: space, message_id: messageId, emoji, ...(on === undefined ? {} : { on }) });

export const MAX_FILE_BYTES = 10 * 1024 * 1024;
export const MAX_FILES_PER_MESSAGE = 10;
export const fileUrl = (id: string) => '/api/agora/file/' + encodeURIComponent(id);

function asBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error('unreadable file'));
    reader.onload = () => { const text = String(reader.result || ''); resolve(text.slice(text.indexOf(',') + 1)); };
    reader.readAsDataURL(blob);
  });
}
/** Send a file to the server so the next message can carry it. */
export async function sendFile(space: string, file: File): Promise<MessageFile> {
  const made = await agoraPost<{ file: MessageFile }>('file_add', {
    space_id: space, name: file.name || 'file', type: file.type || '', content_base64: await asBase64(file) });
  return made.file;
}

/* ── Who is on a server ── */
export const getMembers = (space: string, q = '', signal?: AbortSignal) =>
  agoraGet<{ members: AgoraMember[]; total: number }>('members', { space_id: space, q }, signal);
export const addMember = (space: string, id: string, note = '') =>
  agoraPost<{ member: AgoraMember; added: boolean; registered: boolean }>('member_add', { space_id: space, id, ...(note ? { note } : {}) });
export interface MemberRemoval { removed: string; freed_tasks: number[]; handed_tasks: number[]; handed_to: string | null; left_in_review: number[]; forgotten: boolean }
export const removeMember = (space: string, id: string, opts: { to?: string; reason?: string } = {}) =>
  agoraPost<MemberRemoval>('member_remove', { space_id: space, id, ...(opts.to ? { to_agent: opts.to } : {}), ...(opts.reason ? { reason: opts.reason } : {}) });
export const pruneMembers = (space: string, days: number, dryRun: boolean) =>
  agoraPost<{ removed: string[]; kept: { id: string; why: string }[]; dry_run: boolean; older_than_days: number }>(
    'members_prune', { space_id: space, older_than_days: days, dry_run: dryRun });

/* ── Sessions the hub can start: its console runs the providers' own programs on the PC ── */
export interface ConsoleModel { id: string; name?: string; label?: string; efforts?: string[]; default_effort?: string | null }
export interface ConsoleLeader {
  id: string; name: string; agent_id: string; catalog: ConsoleModel[]; model?: string | null; effort?: string | null;
  detail?: string | null; state?: string | null; auth_state?: string | null;
  /** What the provider's own program on the PC was last seen doing; `auth_expired` when its last call was refused for its sign-in. */
  live_state?: { state?: string | null; detail?: string | null } | null;
  /** The role a session of each model starts with on this server, where the person chose one. */
  model_ranks?: Record<string, string>;
  /** Whether a model can also be asked for by its full name (its program takes any). */
  takes_model_names?: boolean;
}
export interface ConsoleWorker {
  id: string; parent_id: string; agent_id: string; model: string; effort: string; task: string; state: string;
  detail?: string | null; result?: string | null; created_at?: number; updated_at?: number;
}
export const getConsole = (space: string, signal?: AbortSignal) =>
  agoraGet<{ leaders: ConsoleLeader[]; workers: ConsoleWorker[] }>('console/overview', { space_id: space }, signal);
export const launchWorker = (space: string, input: { leader_id: string; model: string; effort: string; task: string; name?: string; rank?: string }) =>
  agoraPost<{ worker: ConsoleWorker; warning?: string }>('console/worker', { space_id: space, action: 'create', ...input });
/** A free name for the next session of a provider on a model: the provider, what tells the model apart and a number. */
export const suggestWorkerName = (space: string, leader: string, model: string, signal?: AbortSignal) =>
  agoraGet<{ name: string }>('console/name', { space_id: space, leader_id: leader, model }, signal);
/** The rank of an agent on a server. The hub takes it from the person's pages only. */
export const setMemberRank = (space: string, member: string, rank: string) =>
  agoraPost<{ member: string; rank: string; rank_level: number; changed: boolean }>('member_rank_set', { space_id: space, member, rank });
export const stopWorker = (space: string, workerId: string) =>
  agoraPost<{ worker_id: string; state: string }>('console/worker', { space_id: space, action: 'stop', worker_id: workerId });
/** The model and effort a provider's agents use on this server: its sessions started from here and its console turns. */
/** The role the sessions of one model start with on a server. The hub takes it only from the person's pages. */
export const setModelRank = (space: string, leader: string, model: string, rank: string) =>
  agoraPost<{ ok: boolean; model_ranks: Record<string, string> }>('console/model_rank', { space_id: space, leader_id: leader, model, rank });
export const setConsolePreferences = (space: string, input: { leader_id: string; model: string; effort: string }) =>
  agoraPost<{ ok: boolean; leader_id: string; model: string; effort: string; verified: boolean; applies_to: string; warning?: string | null }>(
    'console/preferences', { space_id: space, ...input });

/* ── How a server looks ── */
/** A picture as the server's icon: a data URL already made small. */
export const setSpaceIcon = (space: string, dataUrl: string) =>
  agoraPost<{ meta: AgoraSpaceInfo['meta'] }>('space_icon_set', { space_id: space, image_base64: dataUrl });
/** `icon` is `img:<stamp>` when the server has its own picture; the stamp keeps a browser from showing an older one. */
export const spaceIconUrl = (space: string, icon: string) =>
  '/api/agora/space-icon/' + encodeURIComponent(space) + '?v=' + encodeURIComponent(icon.slice(4));
export const reorderChannels = (space: string, order: string[]) =>
  agoraPost<{ order: string[] }>('channels_reorder', { space_id: space, order });

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
/** `heard` leaves out the servers the person silenced: what is worth a notification, not all that waits. */
export const getInbox = (signal?: AbortSignal, heard = false) =>
  agoraGet<PersonInbox>('inbox', { agent: 'luis', all_spaces: 1, peek: 1, mine_only: 1, limit: 40, ...(heard ? { skip_muted: 1 } : {}) }, signal);

/** `s` names the size served, so a browser never shows an older, heavier copy it kept. */
/** `soft`: an app with no icon answers «no content» instead of «not found», for a picture that has letters to fall back on. */
export const iconUrl = (app: string, soft = false) => '/api/agora/icon/' + encodeURIComponent(app) + '?s=128' + (soft ? '&soft=1' : '');
