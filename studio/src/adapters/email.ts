import { ApiError, getJson } from './api';

/**
 * Correo: `/api/email`, the routes emailInbox.js / emailLibrary.js use.
 * Reading, triage (read/unread, flag, archive, delete, move), search and
 * compose/reply. Accounts, rules, AI helpers and scheduling stay in the
 * previous interface for now (PARIDAD).
 */

export interface EmailAccount {
  id: string;
  name: string;
  isDefault: boolean;
  enabled: boolean;
  fromAddress: string;
  displayName: string;
  canSend: boolean;
}

export interface EmailSummary {
  uid: string;
  messageId: string;
  subject: string;
  fromName: string;
  fromAddress: string;
  to: string;
  date: string;
  isRead: boolean;
  isAnswered: boolean;
  isFlagged: boolean;
  hasAttachments: boolean;
  folder: string;
  snippet: string;
}

export interface EmailAttachment {
  index: number;
  filename: string;
  size: number;
  contentType: string;
}

export interface EmailFull extends EmailSummary {
  cc: string;
  body: string;
  bodyHtml: string;
  inReplyTo: string;
  references: string;
  attachments: EmailAttachment[];
}

export type ListFilter = 'all' | 'unread' | 'unanswered' | 'favorites';

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

function summaryFrom(raw: Record<string, unknown>): EmailSummary {
  return {
    uid: String(raw.uid ?? ''),
    messageId: typeof raw.message_id === 'string' ? raw.message_id : '',
    subject: typeof raw.subject === 'string' && raw.subject ? raw.subject : '(sin asunto)',
    fromName: typeof raw.from_name === 'string' ? raw.from_name : '',
    fromAddress: typeof raw.from_address === 'string' ? raw.from_address : '',
    to: typeof raw.to === 'string' ? raw.to : '',
    date: typeof raw.date === 'string' ? raw.date : '',
    isRead: Boolean(raw.is_read),
    isAnswered: Boolean(raw.is_answered),
    isFlagged: Boolean(raw.is_flagged),
    hasAttachments: Boolean(raw.has_attachments),
    folder: typeof raw.folder === 'string' ? raw.folder : 'INBOX',
    snippet: typeof raw.snippet === 'string' ? raw.snippet : typeof raw.preview === 'string' ? raw.preview : '',
  };
}

function q(params: Record<string, string | number | boolean | null | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') sp.set(k, String(v));
  const s = sp.toString();
  return s ? `?${s}` : '';
}

export async function listAccounts(signal?: AbortSignal): Promise<EmailAccount[]> {
  const data = await getJson<{ accounts?: Record<string, unknown>[] }>('/api/email/accounts', signal);
  return (data.accounts ?? []).map((a) => ({
    id: String(a.id ?? ''),
    name: typeof a.name === 'string' ? a.name : 'Cuenta',
    isDefault: Boolean(a.is_default),
    enabled: a.enabled !== false,
    fromAddress: typeof a.from_address === 'string' ? a.from_address : '',
    displayName: typeof a.display_name === 'string' ? a.display_name : '',
    canSend: Boolean(a.smtp_host) || Boolean(a.oauth_provider),
  }));
}

export async function listFolders(accountId: string | null, signal?: AbortSignal): Promise<string[]> {
  const data = await getJson<{ folders?: unknown[] }>(`/api/email/folders${q({ account_id: accountId })}`, signal);
  return (data.folders ?? []).map((f) => (typeof f === 'string' ? f : String((f as { name?: unknown })?.name ?? ''))).filter(Boolean);
}

export interface ListResult {
  emails: EmailSummary[];
  total: number;
  error: string | null;
  source: string;
}

export async function listEmails(opts: { folder: string; accountId: string | null; filter: ListFilter; offset: number; limit: number; refresh?: boolean }, signal?: AbortSignal): Promise<ListResult> {
  const data = await getJson<{ emails?: Record<string, unknown>[]; total?: number; error?: string; sync?: { source?: string } }>(
    `/api/email/list${q({ folder: opts.folder, account_id: opts.accountId, filter: opts.filter, offset: opts.offset, limit: opts.limit, _: opts.refresh ? Date.now() : undefined })}`,
    signal,
  );
  return { emails: (data.emails ?? []).map(summaryFrom), total: Number(data.total ?? 0), error: typeof data.error === 'string' ? data.error : null, source: data.sync?.source ?? '' };
}

export async function searchEmails(query: string, folder: string, accountId: string | null, signal?: AbortSignal): Promise<EmailSummary[]> {
  const data = await getJson<{ emails?: Record<string, unknown>[]; results?: Record<string, unknown>[] }>(`/api/email/search${q({ q: query, folder, account_id: accountId, limit: 50 })}`, signal);
  return (data.emails ?? data.results ?? []).map(summaryFrom);
}

export async function readEmail(uid: string, folder: string, accountId: string | null, signal?: AbortSignal): Promise<EmailFull> {
  const raw = await getJson<Record<string, unknown>>(`/api/email/read/${encodeURIComponent(uid)}${q({ folder, account_id: accountId })}`, signal);
  if (typeof raw.error === 'string' && raw.error) throw new ApiError(raw.error, 404);
  const atts = Array.isArray(raw.attachments) ? (raw.attachments as Record<string, unknown>[]) : [];
  return {
    ...summaryFrom(raw),
    isRead: true,
    cc: typeof raw.cc === 'string' ? raw.cc : '',
    body: typeof raw.body === 'string' ? raw.body : '',
    bodyHtml: typeof raw.body_html === 'string' ? raw.body_html : '',
    inReplyTo: typeof raw.in_reply_to === 'string' ? raw.in_reply_to : '',
    references: typeof raw.references === 'string' ? raw.references : '',
    attachments: atts.map((a, i) => ({
      index: typeof a.index === 'number' ? a.index : i,
      filename: typeof a.filename === 'string' ? a.filename : `adjunto-${i + 1}`,
      size: typeof a.size === 'number' ? a.size : 0,
      contentType: typeof a.content_type === 'string' ? a.content_type : '',
    })),
  };
}

export function attachmentUrl(uid: string, index: number, folder: string, accountId: string | null): string {
  return `/api/email/attachment/${encodeURIComponent(uid)}/${index}${q({ folder, account_id: accountId })}`;
}

async function post(path: string, what: string): Promise<void> {
  await ok(await fetch(path, { method: 'POST', credentials: 'same-origin' }), what);
}

export function markRead(uid: string, folder: string, accountId: string | null): Promise<void> {
  return post(`/api/email/mark-read/${encodeURIComponent(uid)}${q({ folder, account_id: accountId })}`, 'email/mark-read');
}
export function markUnread(uid: string, folder: string, accountId: string | null): Promise<void> {
  return post(`/api/email/mark-unread/${encodeURIComponent(uid)}${q({ folder, account_id: accountId })}`, 'email/mark-unread');
}
export function flagEmail(uid: string, on: boolean, folder: string, accountId: string | null): Promise<void> {
  return post(`/api/email/flag/${encodeURIComponent(uid)}${q({ folder, account_id: accountId, on })}`, 'email/flag');
}
export function archiveEmail(uid: string, folder: string, accountId: string | null): Promise<void> {
  return post(`/api/email/archive/${encodeURIComponent(uid)}${q({ folder, account_id: accountId })}`, 'email/archive');
}
export function moveEmail(uid: string, dest: string, folder: string, accountId: string | null): Promise<void> {
  return post(`/api/email/move/${encodeURIComponent(uid)}${q({ folder, dest, account_id: accountId })}`, 'email/move');
}
export async function deleteEmail(uid: string, folder: string, accountId: string | null): Promise<void> {
  await ok(await fetch(`/api/email/delete/${encodeURIComponent(uid)}${q({ folder, account_id: accountId })}`, { method: 'DELETE', credentials: 'same-origin' }), 'email/delete');
}

export interface Outgoing {
  to: string;
  cc?: string;
  bcc?: string;
  subject: string;
  body: string;
  inReplyTo?: string;
  references?: string;
  accountId?: string | null;
  sourceUid?: string;
  sourceFolder?: string;
}

function outgoingBody(o: Outgoing): string {
  return JSON.stringify({
    to: o.to,
    cc: o.cc || null,
    bcc: o.bcc || null,
    subject: o.subject,
    body: o.body,
    in_reply_to: o.inReplyTo || null,
    references: o.references || null,
    account_id: o.accountId || null,
    source_uid: o.sourceUid || null,
    source_folder: o.sourceFolder || null,
  });
}

export async function sendEmail(o: Outgoing): Promise<void> {
  const r = await ok(await fetch('/api/email/send', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: outgoingBody(o) }), 'email/send');
  const data = (await r.json()) as { success?: boolean; error?: string };
  if (data.success === false) throw new ApiError(data.error || 'No se ha podido enviar.', 400);
}

export async function saveDraft(o: Outgoing): Promise<void> {
  const r = await ok(await fetch('/api/email/draft', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: outgoingBody(o) }), 'email/draft');
  const data = (await r.json()) as { success?: boolean; error?: string };
  if (data.success === false) throw new ApiError(data.error || 'No se ha podido guardar el borrador.', 400);
}

export const FOLDER_LABEL: Record<string, string> = {
  INBOX: 'Bandeja de entrada',
  Archive: 'Archivo',
  Sent: 'Enviados',
  Drafts: 'Borradores',
  Trash: 'Papelera',
  Junk: 'Spam',
  Spam: 'Spam',
};

export function folderLabel(name: string): string {
  return FOLDER_LABEL[name] ?? FOLDER_LABEL[name.split('/').pop() ?? ''] ?? name.replace(/^INBOX[./]/, '');
}

export function quoteFor(mail: EmailFull): string {
  const who = mail.fromName ? `${mail.fromName} <${mail.fromAddress}>` : mail.fromAddress;
  const when = mail.date ? new Date(mail.date).toLocaleString('es') : '';
  const lines = (mail.body || '').split('\n').map((l) => `> ${l}`);
  return `\n\n${when ? `El ${when}, ` : ''}${who} escribió:\n${lines.join('\n')}`;
}
