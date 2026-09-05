import { ApiError, getJson } from './api';
import { t, locale } from '../i18n';

/**
 * Correo: everything under `/api/email` (and the contact search compose
 * needs). Reading, triage, search, AI helpers (summary, translation,
 * drafted replies, writing style), compose with attachments, scheduling
 * and agent drafts waiting for approval, unsubscribe review, the away
 * reply and the automatic features. Accounts themselves are edited in
 * Settings → Integrations (`adapters/integrations.ts`).
 */

export interface EmailAccount {
  id: string;
  name: string;
  isDefault: boolean;
  enabled: boolean;
  fromAddress: string;
  displayName: string;
  canSend: boolean;
  /** Addresses that count as "me" when a thread is drawn as a conversation. */
  aliases: string[];
}

export interface EmailSummary {
  uid: string;
  messageId: string;
  subject: string;
  fromName: string;
  fromAddress: string;
  to: string;
  date: string;
  dateEpoch: number;
  isRead: boolean;
  isAnswered: boolean;
  isFlagged: boolean;
  hasAttachments: boolean;
  folder: string;
  snippet: string;
  tags: string[];
  isSpamVerdict: boolean;
  calendarEventUids: string[];
  size: number;
}

export interface EmailAttachment {
  index: number;
  filename: string;
  size: number;
  contentType: string;
  /** Inline (cid) parts the HTML references; shown only under "images". */
  inline: boolean;
  contentId: string;
}

export interface ThreadTurn {
  level: number;
  bodyHtml: string;
  meta: string;
}

export interface EmailFull extends EmailSummary {
  cc: string;
  body: string;
  bodyHtml: string;
  inReplyTo: string;
  references: string;
  attachments: EmailAttachment[];
  attachmentsDeferred: boolean;
  cachedSummary: string | null;
  cachedAiReply: string | null;
  threadTurns: ThreadTurn[] | null;
  senderSignature: string | null;
}

/** Server filters. `tag:x` also works (`tag:urgent`, `tag:spam`…). */
export type ListFilter = 'all' | 'unread' | 'unanswered' | 'undone' | 'favorites' | 'reminders' | 'pending_30d' | 'stale_30d' | `tag:${string}`;

/** The tags the server's triage task writes, with their labels (English keys). */
export const KNOWN_TAGS: { tag: string; label: string }[] = [
  { tag: 'urgent', label: 'Urgent' },
  { tag: 'reply-soon', label: 'Reply soon' },
  { tag: 'action-needed', label: 'Action needed' },
  { tag: 'bills', label: 'Bills' },
  { tag: 'receipt', label: 'Receipt' },
  { tag: 'travel', label: 'Travel' },
  { tag: 'newsletter', label: 'Newsletter' },
  { tag: 'calendar', label: 'Calendar' },
  { tag: 'spam', label: 'Spam' },
];

export function tagLabel(tag: string): string {
  const known = KNOWN_TAGS.find((k) => k.tag === tag);
  return known ? t(known.label) : tag.replace(/-/g, ' ');
}

async function ok(response: Response, what: string): Promise<Response> {
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: unknown; error?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
      else if (typeof body.error === 'string') detail = body.error;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
  }
  return response;
}

const JSON_HEADERS = { 'Content-Type': 'application/json', Accept: 'application/json' };

async function postJson<T = Record<string, unknown>>(path: string, body: unknown, what: string): Promise<T> {
  const r = await ok(await fetch(path, { method: 'POST', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify(body) }), what);
  return (await r.json()) as T;
}

async function putJson<T = Record<string, unknown>>(path: string, body: unknown, what: string): Promise<T> {
  const r = await ok(await fetch(path, { method: 'PUT', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify(body) }), what);
  return (await r.json()) as T;
}

/** Many mail routes answer 200 with `{success:false,error}`; make that a throw too. */
function must<T extends { success?: boolean; error?: string }>(data: T, fallback: string): T {
  if (data.success === false || (data.error && data.success !== true)) throw new ApiError(data.error || fallback, 400);
  return data;
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.map((x) => String(x ?? '')).filter(Boolean) : [];
}

function summaryFrom(raw: Record<string, unknown>): EmailSummary {
  return {
    uid: String(raw.uid ?? ''),
    messageId: typeof raw.message_id === 'string' ? raw.message_id : '',
    subject: typeof raw.subject === 'string' && raw.subject ? raw.subject : t('(no subject)'),
    fromName: typeof raw.from_name === 'string' ? raw.from_name : '',
    fromAddress: typeof raw.from_address === 'string' ? raw.from_address : '',
    to: typeof raw.to === 'string' ? raw.to : '',
    date: typeof raw.date === 'string' ? raw.date : '',
    dateEpoch: typeof raw.date_epoch === 'number' ? raw.date_epoch : 0,
    isRead: Boolean(raw.is_read),
    isAnswered: Boolean(raw.is_answered),
    isFlagged: Boolean(raw.is_flagged),
    hasAttachments: Boolean(raw.has_attachments),
    folder: typeof raw.folder === 'string' ? raw.folder : 'INBOX',
    snippet: typeof raw.snippet === 'string' ? raw.snippet : typeof raw.preview === 'string' ? raw.preview : '',
    tags: strList(raw.tags),
    isSpamVerdict: Boolean(raw.is_spam_verdict),
    calendarEventUids: strList(raw.calendar_event_uids),
    size: typeof raw.size === 'number' ? raw.size : 0,
  };
}

function q(params: Record<string, string | number | boolean | null | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '' && v !== false) sp.set(k, String(v));
  const s = sp.toString();
  return s ? `?${s}` : '';
}

/* ── Accounts and folders ── */

export async function listAccounts(signal?: AbortSignal): Promise<EmailAccount[]> {
  const data = await getJson<{ accounts?: Record<string, unknown>[] }>('/api/email/accounts', signal);
  return (data.accounts ?? []).map((a) => {
    const from = typeof a.from_address === 'string' ? a.from_address : '';
    const aliases = new Set<string>();
    for (const k of ['from_address', 'imap_user', 'smtp_user']) {
      const v = a[k];
      if (typeof v === 'string' && v.includes('@')) aliases.add(v.toLowerCase().trim());
    }
    return {
      id: String(a.id ?? ''),
      name: typeof a.name === 'string' && a.name ? a.name : from || t('Account'),
      isDefault: Boolean(a.is_default),
      enabled: a.enabled !== false,
      fromAddress: from,
      displayName: typeof a.display_name === 'string' ? a.display_name : '',
      canSend: Boolean(a.smtp_host) || Boolean(a.oauth_provider),
      aliases: [...aliases],
    };
  });
}

export async function listFolders(accountId: string | null, signal?: AbortSignal): Promise<string[]> {
  const data = await getJson<{ folders?: unknown[] }>(`/api/email/folders${q({ account_id: accountId })}`, signal);
  return (data.folders ?? []).map((f) => (typeof f === 'string' ? f : String((f as { name?: unknown })?.name ?? ''))).filter(Boolean);
}

/* ── List, search, read ── */

export interface ListResult {
  emails: EmailSummary[];
  total: number;
  error: string | null;
  source: string;
}

export interface ListQuery {
  folder: string;
  accountId: string | null;
  filter: ListFilter;
  offset: number;
  limit: number;
  from?: string;
  hasAttachments?: boolean;
  refresh?: boolean;
}

export async function listEmails(o: ListQuery, signal?: AbortSignal): Promise<ListResult> {
  const data = await getJson<{ emails?: Record<string, unknown>[]; total?: number; error?: string; sync?: { source?: string } }>(
    `/api/email/list${q({ folder: o.folder, account_id: o.accountId, filter: o.filter, offset: o.offset, limit: o.limit, from: o.from, has_attachments: o.hasAttachments ? 1 : 0, _: o.refresh ? Date.now() : undefined })}`,
    signal,
  );
  return { emails: (data.emails ?? []).map(summaryFrom), total: Number(data.total ?? 0), error: typeof data.error === 'string' ? data.error : null, source: data.sync?.source ?? '' };
}

export type SearchScope = 'all' | 'current' | 'inbox' | 'sent';

export async function searchEmails(query: string, folder: string, accountId: string | null, scope: SearchScope = 'current', signal?: AbortSignal): Promise<EmailSummary[]> {
  const data = await getJson<{ emails?: Record<string, unknown>[]; results?: Record<string, unknown>[] }>(`/api/email/search${q({ q: query, folder, account_id: accountId, limit: 50, scope })}`, signal);
  return (data.emails ?? data.results ?? []).map(summaryFrom);
}

export interface UnreadState {
  unreadCount: number;
  maxUid: number;
}

export async function unreadState(folder: string, accountId: string | null, signal?: AbortSignal): Promise<UnreadState> {
  const d = await getJson<{ unread_count?: number; max_uid?: number }>(`/api/email/unread-state${q({ folder, account_id: accountId })}`, signal);
  return { unreadCount: Number(d.unread_count ?? 0), maxUid: Number(d.max_uid ?? 0) };
}

export interface UrgencyVerdict {
  score: number;
  reason: string;
}

/** `per_uid` keys are `account:uid`; this flattens them to uid → verdict. */
export async function urgencyByUid(signal?: AbortSignal): Promise<Map<string, UrgencyVerdict>> {
  const out = new Map<string, UrgencyVerdict>();
  try {
    const d = await getJson<{ per_uid?: Record<string, { score?: number; reason?: string }> }>('/api/email/urgency-state', signal);
    for (const [key, v] of Object.entries(d.per_uid ?? {})) {
      const uid = key.includes(':') ? key.slice(key.indexOf(':') + 1) : key;
      const score = Number(v?.score ?? 0);
      if (score >= 2) out.set(uid, { score, reason: typeof v?.reason === 'string' ? v.reason : '' });
    }
  } catch {
    /* no urgency task ran yet */
  }
  return out;
}

function attachmentsFrom(raw: unknown, inline: boolean): EmailAttachment[] {
  const atts = Array.isArray(raw) ? (raw as Record<string, unknown>[]) : [];
  return atts.map((a, i) => ({
    index: typeof a.index === 'number' ? a.index : i,
    filename: typeof a.filename === 'string' && a.filename ? a.filename : t('attachment {n}', { n: i + 1 }),
    size: typeof a.size === 'number' ? a.size : 0,
    contentType: typeof a.content_type === 'string' ? a.content_type : '',
    inline,
    contentId: typeof a.content_id === 'string' ? a.content_id.replace(/^<|>$/g, '') : '',
  }));
}

export async function readEmail(uid: string, folder: string, accountId: string | null, signal?: AbortSignal): Promise<EmailFull> {
  const raw = await getJson<Record<string, unknown>>(`/api/email/read/${encodeURIComponent(uid)}${q({ folder, account_id: accountId })}`, signal);
  if (typeof raw.error === 'string' && raw.error) throw new ApiError(raw.error, 404);
  const turns = Array.isArray(raw.thread_turns)
    ? (raw.thread_turns as Record<string, unknown>[]).map((x) => ({ level: Number(x.level ?? 0), bodyHtml: typeof x.body_html === 'string' ? x.body_html : '', meta: typeof x.meta === 'string' ? x.meta : '' }))
    : null;
  return {
    ...summaryFrom(raw),
    folder: typeof raw.folder === 'string' && raw.folder ? raw.folder : folder,
    isRead: true,
    cc: typeof raw.cc === 'string' ? raw.cc : '',
    body: typeof raw.body === 'string' ? raw.body : '',
    bodyHtml: typeof raw.body_html === 'string' ? raw.body_html : '',
    inReplyTo: typeof raw.in_reply_to === 'string' ? raw.in_reply_to : '',
    references: typeof raw.references === 'string' ? raw.references : '',
    attachments: [...attachmentsFrom(raw.attachments, false), ...attachmentsFrom(raw.related_attachments, true)],
    attachmentsDeferred: Boolean(raw.attachments_deferred),
    cachedSummary: typeof raw.cached_summary === 'string' && raw.cached_summary ? raw.cached_summary : null,
    cachedAiReply: typeof raw.cached_ai_reply === 'string' && raw.cached_ai_reply ? raw.cached_ai_reply : null,
    threadTurns: turns && turns.length ? turns : null,
    senderSignature: typeof raw.sender_signature === 'string' && raw.sender_signature ? raw.sender_signature : null,
  };
}

/** Attachments arrive later for big mails (`attachments_deferred`). */
export async function listAttachments(uid: string, folder: string, accountId: string | null, signal?: AbortSignal): Promise<EmailAttachment[]> {
  const d = await getJson<{ attachments?: unknown; error?: string }>(`/api/email/attachments/${encodeURIComponent(uid)}${q({ folder, account_id: accountId })}`, signal);
  if (d.error) throw new ApiError(d.error, 404);
  return attachmentsFrom(d.attachments, false);
}

export function attachmentUrl(uid: string, index: number, folder: string, accountId: string | null): string {
  return `/api/email/attachment/${encodeURIComponent(uid)}/${index}${q({ folder, account_id: accountId })}`;
}

export function attachmentsZipUrl(uid: string, folder: string, accountId: string | null): string {
  return `/api/email/attachments-download/${encodeURIComponent(uid)}${q({ folder, account_id: accountId })}`;
}

export function inlineImageUrl(uid: string, cid: string, folder: string, accountId: string | null): string {
  return `/api/email/inline-image/${encodeURIComponent(uid)}${q({ cid, folder, account_id: accountId })}`;
}

/** Turns an attachment (pdf, docx, text, eml) into a document of the library. */
export async function attachmentAsDoc(uid: string, index: number, folder: string, accountId: string | null): Promise<{ docId: string; filename: string }> {
  const d = await postJson<{ doc_id?: string; filename?: string; error?: string }>(`/api/email/attachment-as-doc/${encodeURIComponent(uid)}/${index}${q({ folder, account_id: accountId })}`, {}, 'email/attachment-as-doc');
  if (d.error || !d.doc_id) throw new ApiError(d.error || t('Could not open the attachment as a document.'), 400);
  return { docId: d.doc_id, filename: d.filename ?? '' };
}

/* ── Triage ── */

async function post(path: string, what: string): Promise<void> {
  const r = await ok(await fetch(path, { method: 'POST', credentials: 'same-origin' }), what);
  try {
    const d = (await r.clone().json()) as { ok?: boolean; success?: boolean; error?: string };
    if (d && (d.ok === false || d.success === false)) throw new ApiError(d.error || t('The operation failed.'), 400);
  } catch (err) {
    if (err instanceof ApiError) throw err;
  }
}

const enc = encodeURIComponent;
type Ctx = { folder: string; accountId: string | null };
const ctx = (c: Ctx, extra: Record<string, string | number | boolean | null | undefined> = {}) => q({ folder: c.folder, account_id: c.accountId, ...extra });

export const markRead = (uid: string, c: Ctx) => post(`/api/email/mark-read/${enc(uid)}${ctx(c)}`, 'email/mark-read');
export const markUnread = (uid: string, c: Ctx) => post(`/api/email/mark-unread/${enc(uid)}${ctx(c)}`, 'email/mark-unread');
export const flagEmail = (uid: string, on: boolean, c: Ctx) => post(`/api/email/flag/${enc(uid)}${ctx(c, { on })}`, 'email/flag');
export const archiveEmail = (uid: string, c: Ctx) => post(`/api/email/archive/${enc(uid)}${ctx(c)}`, 'email/archive');
export const moveEmail = (uid: string, dest: string, c: Ctx) => post(`/api/email/move/${enc(uid)}${ctx(c, { dest })}`, 'email/move');
/** "Done": answered flag on, and read. */
export async function markDone(uid: string, c: Ctx): Promise<void> {
  await post(`/api/email/mark-answered/${enc(uid)}${ctx(c)}`, 'email/mark-answered');
  await post(`/api/email/mark-read/${enc(uid)}${ctx(c)}`, 'email/mark-read');
}
export const markNotDone = (uid: string, c: Ctx) => post(`/api/email/clear-answered/${enc(uid)}${ctx(c)}`, 'email/clear-answered');
export const notSpam = (uid: string) => post(`/api/email/${enc(uid)}/unflag-spam`, 'email/unflag-spam');

export async function deleteEmail(uid: string, c: Ctx, permanent = false): Promise<void> {
  await ok(await fetch(`/api/email/${permanent ? 'delete-permanent' : 'delete'}/${enc(uid)}${ctx(c)}`, { method: 'DELETE', credentials: 'same-origin' }), 'email/delete');
}

/** Removes the reminder mails Faustus sent itself (the "Reminders" filter). */
export async function deleteReminderMails(accountId: string | null, permanent = false): Promise<number> {
  const r = await ok(await fetch(`/api/email/odysseus/reminders${q({ account_id: accountId, permanent })}`, { method: 'DELETE', credentials: 'same-origin' }), 'email/reminders');
  const d = must((await r.json()) as { success?: boolean; error?: string; deleted?: number }, t('Could not delete the reminders.'));
  return Number(d.deleted ?? 0);
}

/* ── AI ── */

export interface AiTarget {
  uid: string;
  folder: string;
  accountId: string | null;
  messageId: string;
  subject: string;
  from: string;
  body: string;
}

export async function summarizeEmail(m: AiTarget): Promise<{ summary: string; model: string }> {
  const d = must(
    await postJson<{ success?: boolean; error?: string; summary?: string; model_used?: string }>('/api/email/summarize', { body: m.body, subject: m.subject, from: m.from, uid: m.uid, folder: m.folder, message_id: m.messageId, account_id: m.accountId ?? '' }, 'email/summarize'),
    t('Could not summarise.'),
  );
  return { summary: d.summary ?? '', model: d.model_used ?? '' };
}

export async function translateEmail(m: AiTarget, targetLanguage: string): Promise<{ translation: string; sameLanguage: boolean; model: string }> {
  const d = must(
    await postJson<{ success?: boolean; error?: string; translation?: string; same_language?: boolean; model_used?: string }>('/api/email/translate', { body: m.body, subject: m.subject, from: m.from, uid: m.uid, folder: m.folder, target_language: targetLanguage }, 'email/translate'),
    t('Could not translate.'),
  );
  return { translation: d.translation ?? '', sameLanguage: Boolean(d.same_language), model: d.model_used ?? '' };
}

export interface AiReplyInput {
  to: string;
  subject: string;
  originalBody: string;
  uid?: string;
  folder?: string;
  accountId?: string | null;
  messageId?: string;
  fast: boolean;
  hint: string;
  model?: string;
}

export async function aiReply(i: AiReplyInput): Promise<{ reply: string; model: string }> {
  const d = must(
    await postJson<{ success?: boolean; error?: string; reply?: string; model_used?: string }>(
      '/api/email/ai-reply',
      { to: i.to, subject: i.subject, original_body: i.originalBody, uid: i.uid ?? '', folder: i.folder ?? 'INBOX', account_id: i.accountId ?? '', message_id: i.messageId ?? '', fast: i.fast, user_hint: i.hint, model: i.model ?? '' },
      'email/ai-reply',
    ),
    t('Could not draft the reply.'),
  );
  return { reply: d.reply ?? '', model: d.model_used ?? '' };
}

export async function getWritingStyle(accountId: string | null): Promise<string> {
  const d = await getJson<{ style?: string }>(`/api/email/style${q({ account_id: accountId })}`);
  return d.style ?? '';
}

export async function saveWritingStyle(accountId: string | null, style: string): Promise<void> {
  must(await putJson<{ success?: boolean; error?: string }>(`/api/email/style${q({ account_id: accountId })}`, { style }, 'email/style'), t('Could not save the style.'));
}

export async function extractWritingStyle(accountId: string | null, sampleCount = 15): Promise<string> {
  const d = must(await postJson<{ success?: boolean; error?: string; style?: string }>(`/api/email/extract-style${q({ account_id: accountId })}`, { sample_count: sampleCount }, 'email/extract-style'), t('Could not extract the style.'));
  return d.style ?? '';
}

/* ── Config: away reply, automatic features, translation language ── */

export interface MailConfig {
  autoReply: boolean;
  autoReplyStart: string;
  autoReplyEnd: string;
  autoReplySubject: string;
  autoReplyMessage: string;
  autoReplyCooldown: 'period' | '1d' | '3d' | '7d';
  autoReplyExcludeAutomated: boolean;
  autoReplyPauseNotifications: boolean;
  autoSummarize: boolean;
  autoTag: boolean;
  autoSpam: boolean;
  autoCalendar: boolean;
  translateLanguage: string;
}

export const DEFAULT_AWAY_SUBJECT = '(Away) {subject}';

export async function getMailConfig(accountId: string | null): Promise<MailConfig> {
  const c = await getJson<Record<string, unknown>>(`/api/email/config${q({ account_id: accountId })}`);
  const s = (k: string, d = '') => (typeof c[k] === 'string' ? (c[k] as string) : d);
  const cooldown = s('email_auto_reply_cooldown', 'period');
  return {
    autoReply: Boolean(c.email_auto_reply),
    autoReplyStart: s('email_auto_reply_start').slice(0, 10),
    autoReplyEnd: s('email_auto_reply_end').slice(0, 10),
    autoReplySubject: s('email_auto_reply_subject', DEFAULT_AWAY_SUBJECT),
    autoReplyMessage: s('email_auto_reply_message'),
    autoReplyCooldown: (['period', '1d', '3d', '7d'].includes(cooldown) ? cooldown : 'period') as MailConfig['autoReplyCooldown'],
    autoReplyExcludeAutomated: c.email_auto_reply_exclude_automated !== false,
    autoReplyPauseNotifications: Boolean(c.email_auto_reply_pause_notifications),
    autoSummarize: Boolean(c.email_auto_summarize),
    autoTag: Boolean(c.email_auto_tag),
    autoSpam: Boolean(c.email_auto_spam),
    autoCalendar: Boolean(c.email_auto_calendar),
    translateLanguage: s('email_translate_language', 'English'),
  };
}

export async function saveMailConfig(accountId: string | null, cfg: MailConfig): Promise<void> {
  const body = {
    email_auto_reply: cfg.autoReply,
    email_auto_reply_start: cfg.autoReplyStart,
    email_auto_reply_end: cfg.autoReplyEnd,
    email_auto_reply_subject: cfg.autoReplySubject || DEFAULT_AWAY_SUBJECT,
    email_auto_reply_message: cfg.autoReplyMessage,
    email_auto_reply_cooldown: cfg.autoReplyCooldown,
    email_auto_reply_scope: accountId ? 'account' : 'all',
    email_auto_reply_account_id: accountId ?? '',
    email_auto_reply_exclude_automated: cfg.autoReplyExcludeAutomated,
    email_auto_reply_pause_notifications: cfg.autoReplyPauseNotifications,
    email_auto_summarize: cfg.autoSummarize,
    email_auto_tag: cfg.autoTag,
    email_auto_spam: cfg.autoSpam,
    email_auto_calendar: cfg.autoCalendar,
    email_translate_language: cfg.translateLanguage,
  };
  must(await putJson<{ success?: boolean; error?: string }>(`/api/email/config${q({ account_id: accountId })}`, body, 'email/config'), t('Could not save.'));
}

/** Whether the away reply is on for today (for the banner). */
export function awayReplyActive(cfg: MailConfig | null): boolean {
  if (!cfg || !cfg.autoReply) return false;
  const today = new Date().toISOString().slice(0, 10);
  if (cfg.autoReplyStart && today < cfg.autoReplyStart) return false;
  if (cfg.autoReplyEnd && today > cfg.autoReplyEnd) return false;
  return true;
}

/* ── Compose: attachments, contacts, send, draft, schedule ── */

export interface StagedAttachment {
  token: string;
  filename: string;
  size: number;
}

export async function uploadAttachment(file: File): Promise<StagedAttachment> {
  const fd = new FormData();
  fd.append('file', file);
  const r = await ok(await fetch('/api/email/compose-upload', { method: 'POST', credentials: 'same-origin', body: fd }), 'email/compose-upload');
  const d = must((await r.json()) as { success?: boolean; error?: string; token?: string; filename?: string; size?: number }, t('Could not attach the file.'));
  return { token: d.token ?? '', filename: d.filename ?? file.name, size: d.size ?? file.size };
}

/** A library document or a gallery image, staged by the server. */
export async function attachFromLibrary(kind: 'document' | 'gallery', id: string): Promise<StagedAttachment> {
  const d = must(await postJson<{ success?: boolean; error?: string; token?: string; filename?: string; size?: number }>('/api/email/compose-from-odysseus', { kind, id }, 'email/compose-from-odysseus'), t('Could not attach.'));
  return { token: d.token ?? '', filename: d.filename ?? '', size: d.size ?? 0 };
}

export async function attachManyAsZip(items: { kind: 'document' | 'gallery'; id: string }[]): Promise<StagedAttachment> {
  const d = must(await postJson<{ success?: boolean; error?: string; token?: string; filename?: string; size?: number }>('/api/email/compose-from-odysseus-zip', { items }, 'email/compose-from-odysseus-zip'), t('Could not attach.'));
  return { token: d.token ?? '', filename: d.filename ?? 'attachments.zip', size: d.size ?? 0 };
}

/** Re-attach a file that came with a received mail (forwarding). */
export async function attachFromMail(uid: string, index: number, c: Ctx): Promise<StagedAttachment> {
  const d = must(await postJson<{ success?: boolean; error?: string; token?: string; filename?: string; size?: number }>(`/api/email/compose-from-attachment/${enc(uid)}/${index}${ctx(c)}`, {}, 'email/compose-from-attachment'), t('Could not attach.'));
  return { token: d.token ?? '', filename: d.filename ?? '', size: d.size ?? 0 };
}

export async function discardAttachment(token: string): Promise<void> {
  await fetch(`/api/email/compose-upload/${enc(token)}`, { method: 'DELETE', credentials: 'same-origin' }).catch(() => undefined);
}

export interface ContactHit {
  name: string;
  email: string;
}

export async function searchContacts(query: string, signal?: AbortSignal): Promise<ContactHit[]> {
  if (query.trim().length < 2) return [];
  try {
    const d = await getJson<{ results?: { name?: string; emails?: string[]; email?: string }[] }>(`/api/contacts/search${q({ q: query })}`, signal);
    const out: ContactHit[] = [];
    for (const c of d.results ?? []) {
      const emails = Array.isArray(c.emails) && c.emails.length ? c.emails : c.email ? [c.email] : [];
      for (const e of emails) out.push({ name: c.name ?? '', email: e });
    }
    return out.slice(0, 8);
  } catch {
    return [];
  }
}

export async function rememberContact(name: string, email: string): Promise<void> {
  await fetch('/api/contacts/add', { method: 'POST', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify({ name, email }) }).catch(() => undefined);
}

export interface Outgoing {
  to: string;
  cc?: string;
  bcc?: string;
  subject: string;
  body: string;
  bodyHtml?: string;
  inReplyTo?: string;
  references?: string;
  accountId?: string | null;
  sourceUid?: string;
  sourceFolder?: string;
  attachments?: string[];
}

function outgoingBody(o: Outgoing): Record<string, unknown> {
  return {
    to: o.to,
    cc: o.cc || null,
    bcc: o.bcc || null,
    subject: o.subject,
    body: o.body,
    body_html: o.bodyHtml || null,
    in_reply_to: o.inReplyTo || null,
    references: o.references || null,
    account_id: o.accountId || null,
    source_uid: o.sourceUid || null,
    source_folder: o.sourceFolder || null,
    attachments: o.attachments && o.attachments.length ? o.attachments : null,
  };
}

export async function sendEmail(o: Outgoing): Promise<void> {
  must(await postJson<{ success?: boolean; error?: string }>('/api/email/send', outgoingBody(o), 'email/send'), t('Could not send.'));
}

export async function saveDraft(o: Outgoing): Promise<void> {
  must(await postJson<{ success?: boolean; error?: string }>('/api/email/draft', outgoingBody(o), 'email/draft'), t('Could not save the draft.'));
}

/** `sendAt` is a local Date; the server wants ISO-8601 UTC. */
export async function scheduleEmail(o: Outgoing, sendAt: Date): Promise<string> {
  const d = must(await postJson<{ success?: boolean; error?: string; id?: string }>('/api/email/schedule', { ...outgoingBody(o), send_at: sendAt.toISOString(), attachments: o.attachments ?? [] }, 'email/schedule'), t('Could not schedule.'));
  return d.id ?? '';
}

export interface ScheduledMail {
  id: string;
  to: string;
  cc: string;
  subject: string;
  sendAt: string;
  createdAt: string;
  status: string;
  error: string;
}

/** The server stores naive UTC; give it back its `Z` so dates read in local time. */
function utcStamp(v: unknown): string {
  const s = String(v ?? '');
  return s && !/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? `${s}Z` : s;
}

export async function listScheduled(signal?: AbortSignal): Promise<ScheduledMail[]> {
  const d = await getJson<{ scheduled?: Record<string, unknown>[] }>('/api/email/scheduled', signal);
  return (d.scheduled ?? []).map((r) => ({
    id: String(r.id ?? ''),
    to: String(r.to ?? ''),
    cc: String(r.cc ?? ''),
    subject: String(r.subject ?? ''),
    sendAt: utcStamp(r.send_at),
    createdAt: utcStamp(r.created_at),
    status: String(r.status ?? 'pending'),
    error: String(r.error ?? ''),
  }));
}

export async function cancelScheduled(id: string): Promise<void> {
  const r = await ok(await fetch(`/api/email/scheduled/${enc(id)}`, { method: 'DELETE', credentials: 'same-origin' }), 'email/scheduled');
  must((await r.json()) as { success?: boolean; error?: string }, t('Could not cancel.'));
}

export interface PendingDraft {
  id: string;
  to: string;
  subject: string;
  body: string;
  createdAt: string;
  accountId: string;
}

/** Mails an agent wrote and is waiting for a person to approve. */
export async function listPendingDrafts(signal?: AbortSignal): Promise<PendingDraft[]> {
  const d = await getJson<{ pending?: Record<string, unknown>[] }>('/api/email/pending', signal);
  return (d.pending ?? []).map((r) => ({
    id: String(r.id ?? ''),
    to: String(r.to_addr ?? r.to ?? ''),
    subject: String(r.subject ?? ''),
    body: String(r.body ?? ''),
    createdAt: utcStamp(r.created_at),
    accountId: String(r.account_id ?? ''),
  }));
}

export async function approvePendingDraft(id: string): Promise<void> {
  must(await postJson<{ success?: boolean; error?: string }>(`/api/email/pending/${enc(id)}/approve`, {}, 'email/pending'), t('Could not approve.'));
}

export async function discardPendingDraft(id: string): Promise<void> {
  const r = await ok(await fetch(`/api/email/pending/${enc(id)}`, { method: 'DELETE', credentials: 'same-origin' }), 'email/pending');
  must((await r.json()) as { success?: boolean; error?: string }, t('Could not discard.'));
}

/* ── Unsubscribe review ── */

export interface UnsubscribeMethod {
  kind: 'mailto' | 'url';
  target: string;
  executable: boolean;
}

export interface UnsubscribeCandidate {
  uid: string;
  folder: string;
  subject: string;
  fromName: string;
  fromAddress: string;
  listId: string;
  score: number;
  reasons: string[];
  methods: UnsubscribeMethod[];
  canExecute: boolean;
  duplicateCount: number;
  spamReason: string;
}

export async function scanUnsubscribe(folder: string, accountId: string | null, signal?: AbortSignal): Promise<{ candidates: UnsubscribeCandidate[]; scanned: number }> {
  const d = await getJson<{ success?: boolean; error?: string; candidates?: Record<string, unknown>[]; scanned?: number }>(`/api/email/unsubscribe/scan${q({ folder, account_id: accountId, limit: 40, max_scan: 200 })}`, signal);
  must(d, t('Could not scan the folder.'));
  return {
    scanned: Number(d.scanned ?? 0),
    candidates: (d.candidates ?? []).map((c) => ({
      uid: String(c.uid ?? ''),
      folder: String(c.folder ?? folder),
      subject: String(c.subject ?? ''),
      fromName: String(c.from_name ?? ''),
      fromAddress: String(c.from_address ?? ''),
      listId: String(c.list_id ?? ''),
      score: Number(c.score ?? 0),
      reasons: strList(c.reasons),
      methods: (Array.isArray(c.methods) ? (c.methods as Record<string, unknown>[]) : []).map((m) => ({ kind: m.kind === 'url' ? 'url' : 'mailto', target: String(m.target ?? ''), executable: Boolean(m.executable) })),
      canExecute: Boolean(c.can_execute),
      duplicateCount: Number(c.duplicate_count ?? 1),
      spamReason: String(c.spam_reason ?? ''),
    })),
  };
}

export async function executeUnsubscribe(c: UnsubscribeCandidate, accountId: string | null, moveToSpam: boolean): Promise<void> {
  const methodIndex = Math.max(0, c.methods.findIndex((m) => m.executable));
  must(await postJson<{ success?: boolean; error?: string }>('/api/email/unsubscribe/execute', { uid: c.uid, folder: c.folder, account_id: accountId, method_index: methodIndex, move_to_spam: moveToSpam }, 'email/unsubscribe'), t('Could not unsubscribe.'));
}

export async function cleanupUnsubscribed(uids: string[], folder: string, accountId: string | null, action: 'junk' | 'delete'): Promise<{ changed: number; failed: number }> {
  const d = must(await postJson<{ success?: boolean; error?: string; changed?: number; failed?: number }>('/api/email/unsubscribe/cleanup', { uids, folder, account_id: accountId, action }, 'email/unsubscribe/cleanup'), t('Could not clean up.'));
  return { changed: Number(d.changed ?? 0), failed: Number(d.failed ?? 0) };
}

/* ── Labels, quoting, hand-offs ── */

export const FOLDER_LABEL: Record<string, string> = {
  INBOX: 'Inbox',
  Archive: 'Archive#folder',
  Sent: 'Sent',
  Drafts: 'Drafts',
  Trash: 'Trash',
  Junk: 'Spam',
  Spam: 'Spam',
};

export function folderLabel(name: string): string {
  const known = FOLDER_LABEL[name] ?? FOLDER_LABEL[name.split('/').pop() ?? ''];
  return known ? t(known) : name.replace(/^INBOX[./]/, '');
}

export function quoteFor(mail: Pick<EmailFull, 'fromName' | 'fromAddress' | 'date' | 'body'>): string {
  const who = mail.fromName ? `${mail.fromName} <${mail.fromAddress}>` : mail.fromAddress;
  const when = mail.date ? new Date(mail.date).toLocaleString(locale()) : '';
  const lines = (mail.body || '').split('\n').map((l) => `> ${l}`);
  return `\n\n${when ? t('On {when}, ', { when }) : ''}${t('{who} wrote:', { who })}\n${lines.join('\n')}`;
}

/** Other screens (the document editor) leave a draft under this key and open `/email?compose=handoff`. */
export const COMPOSE_HANDOFF_KEY = 'fs-compose-handoff';

export interface ComposeHandoff {
  to?: string;
  cc?: string;
  bcc?: string;
  subject?: string;
  body?: string;
  inReplyTo?: string;
  references?: string;
  /** A file already staged on the server (a signed PDF), by upload token. */
  attachmentToken?: string;
  attachmentName?: string;
}

export function leaveComposeHandoff(draft: ComposeHandoff): void {
  sessionStorage.setItem(COMPOSE_HANDOFF_KEY, JSON.stringify(draft));
}

export function takeComposeHandoffRaw(): ComposeHandoff | null {
  try {
    if (!/[?&]compose=handoff/.test(window.location.search)) return null;
    const raw = sessionStorage.getItem(COMPOSE_HANDOFF_KEY);
    if (!raw) return null;
    sessionStorage.removeItem(COMPOSE_HANDOFF_KEY);
    return JSON.parse(raw) as ComposeHandoff;
  } catch {
    return null;
  }
}
