import {
  Archive,
  ArrowLeft,
  Forward,
  Inbox,
  Mail,
  MailOpen,
  Paperclip,
  PenLine,
  RefreshCw,
  Reply,
  ReplyAll,
  Search,
  Settings2,
  Star,
  StarOff,
  Trash2,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Dialog, EmptyState, IconButton, QuickMenu, Skeleton, Toast } from '../components';
import { relativeTime } from '../adapters/home';
import {
  archiveEmail,
  attachmentUrl,
  deleteEmail,
  flagEmail,
  folderLabel,
  listAccounts,
  listEmails,
  listFolders,
  markRead,
  markUnread,
  moveEmail,
  quoteFor,
  readEmail,
  saveDraft,
  searchEmails,
  sendEmail,
  type EmailAccount,
  type EmailFull,
  type EmailSummary,
  type ListFilter,
  type Outgoing,
  takeComposeHandoffRaw,
} from '../adapters/email';
import './projects.css';
import './email.css';
import { t, locale } from '../i18n';

/**
 * Correo (the previous interface's mail window, `/email`).
 *
 * Three panes: accounts and folders, the list (filters, search, paging),
 * the message (HTML in a sandboxed frame, attachments, reply / reply all /
 * forward, archive, delete, move, flag, unread). Compose sends through the
 * same `/send` and `/draft`. What stays in the previous interface (PARIDAD):
 * account setup and OAuth, AI reply / summary / translation, style,
 * scheduling and approvals, unsubscribe, rules and tags.
 */

const PAGE = 40;

function fmtAddr(name: string, address: string): string {
  return name && name !== address ? name : address || t('(unknown)');
}

function fmtWhen(date: string): string {
  if (!date) return '';
  const d = new Date(date);
  if (Number.isNaN(d.getTime())) return date;
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return d.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' });
  if (d.getFullYear() === today.getFullYear()) return d.toLocaleDateString(locale(), { day: 'numeric', month: 'short' });
  return d.toLocaleDateString(locale(), { day: 'numeric', month: 'short', year: 'numeric' });
}

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/* ── Compose ── */

function takeComposeHandoff(): ComposeState | null {
  const d = takeComposeHandoffRaw();
  if (!d) return null;
  return { to: d.to ?? '', cc: d.cc ?? '', bcc: d.bcc ?? '', subject: d.subject ?? '', body: d.body ?? '', inReplyTo: d.inReplyTo, references: d.references };
}

interface ComposeState {
  to: string;
  cc: string;
  bcc: string;
  subject: string;
  body: string;
  inReplyTo?: string;
  references?: string;
  sourceUid?: string;
  sourceFolder?: string;
}

function ComposeDialog({ initial, accounts, accountId, onClose, onDone, say }: { initial: ComposeState; accounts: EmailAccount[]; accountId: string | null; onClose: () => void; onDone: (what: 'sent' | 'draft') => void; say: (t: string) => void }) {
  const [s, setS] = useState(initial);
  const [from, setFrom] = useState(accountId ?? accounts.find((a) => a.isDefault)?.id ?? accounts[0]?.id ?? '');
  const [busy, setBusy] = useState<'send' | 'draft' | null>(null);
  const [showCc, setShowCc] = useState(Boolean(initial.cc || initial.bcc));
  const set = (p: Partial<ComposeState>) => setS((c) => ({ ...c, ...p }));

  const outgoing = (): Outgoing => ({ to: s.to.trim(), cc: s.cc.trim(), bcc: s.bcc.trim(), subject: s.subject.trim(), body: s.body, inReplyTo: s.inReplyTo, references: s.references, accountId: from || null, sourceUid: s.sourceUid, sourceFolder: s.sourceFolder });

  const send = async () => {
    if (!s.to.trim()) {
      say(t('The recipient is missing.'));
      return;
    }
    setBusy('send');
    try {
      await sendEmail(outgoing());
      onDone('sent');
    } catch (err) {
      say((err as Error).message || t('Could not send.'));
    } finally {
      setBusy(null);
    }
  };

  const draft = async () => {
    setBusy('draft');
    try {
      await saveDraft(outgoing());
      onDone('draft');
    } catch (err) {
      say((err as Error).message || t('Could not save the draft.'));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      title={s.inReplyTo ? t('Reply') : s.subject.startsWith('Fwd:') ? t('Forward') : t('New mail')}
      testId="compose"
      footer={
        <div className="fs-mail-compose__foot">
          <Button variant="ghost" size="sm" label={t('Save draft')} loading={busy === 'draft'} onClick={() => void draft()} />
          <span className="fs-mail__spacer" />
          <Button variant="ghost" size="sm" label={t('Discard')} onClick={onClose} />
          <Button variant="primary" size="sm" label={t('Send')} loading={busy === 'send'} onClick={() => void send()} testId="compose-send" />
        </div>
      }
    >
      <div className="fs-mail-compose">
        {accounts.length > 1 && (
          <label className="fs-mail-compose__row">
            <span>Desde</span>
            <select className="fs-field" value={from} onChange={(e) => setFrom(e.target.value)}>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} {a.fromAddress ? `<${a.fromAddress}>` : ''}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="fs-mail-compose__row">
          <span>Para</span>
          <input type="text" className="fs-field" value={s.to} onChange={(e) => set({ to: e.target.value })} placeholder={t('name@domain, other@domain')} autoFocus={!s.to} />
          {!showCc && (
            <button type="button" className="fs-mail__link" onClick={() => setShowCc(true)}>
              CC / CCO
            </button>
          )}
        </label>
        {showCc && (
          <>
            <label className="fs-mail-compose__row">
              <span>CC</span>
              <input type="text" className="fs-field" value={s.cc} onChange={(e) => set({ cc: e.target.value })} />
            </label>
            <label className="fs-mail-compose__row">
              <span>CCO</span>
              <input type="text" className="fs-field" value={s.bcc} onChange={(e) => set({ bcc: e.target.value })} />
            </label>
          </>
        )}
        <label className="fs-mail-compose__row">
          <span>Asunto</span>
          <input type="text" className="fs-field" value={s.subject} onChange={(e) => set({ subject: e.target.value })} />
        </label>
        <textarea className="fs-mail-compose__body" rows={12} value={s.body} onChange={(e) => set({ body: e.target.value })} autoFocus={Boolean(s.to)} placeholder={t('Write here. Markdown works.')} />
      </div>
    </Dialog>
  );
}

/* ── Message pane ── */

function Message({ mail, folders, accountId, onBack, onChanged, onCompose, say }: { mail: EmailFull; folders: string[]; accountId: string | null; onBack: () => void; onChanged: (patch: Partial<EmailSummary> | 'gone') => void; onCompose: (c: ComposeState) => void; say: (t: string) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [frameH, setFrameH] = useState(320);

  const html = useMemo(() => {
  const base = '<base target="_blank"><style>body{margin:0;padding:12px 14px;font:14px/1.5 system-ui,sans-serif;color:#1b1b1f;background:#fff;overflow-wrap:anywhere}img{max-width:100%;height:auto}pre{white-space:pre-wrap}blockquote{border-left:3px solid #ccc;margin:8px 0;padding-left:10px;color:#555}</style>'; // guard-ok: colours inside the sandboxed mail document, not the UI
    return mail.bodyHtml ? mail.bodyHtml.replace(/<head[^>]*>/i, (m) => `${m}${base}`).replace(/^(?![\s\S]*<head)/i, base) : `<!doctype html><html><head>${base}</head><body><pre>${mail.body.replace(/&/g, '&amp;').replace(/</g, '&lt;')}</pre></body></html>`;
  }, [mail]);

  const onFrameLoad = () => {
    try {
      const doc = frameRef.current?.contentDocument;
      if (doc) setFrameH(Math.min(2400, Math.max(160, doc.documentElement.scrollHeight + 8)));
    } catch {
      /* cross-origin never happens with srcdoc */
    }
  };

  const act = async (what: string, fn: () => Promise<void>, after: Partial<EmailSummary> | 'gone', okText: string) => {
    setBusy(what);
    try {
      await fn();
      onChanged(after);
      say(okText);
    } catch (err) {
      say((err as Error).message || t('The operation failed.'));
    } finally {
      setBusy(null);
    }
  };

  const replyState = (all: boolean): ComposeState => {
    const others = all ? [mail.to, mail.cc].filter((x) => x && !x.includes(mail.fromAddress)).join(', ') : '';
    return {
      to: mail.fromAddress || mail.fromName,
      cc: others,
      bcc: '',
      subject: /^re:/i.test(mail.subject) ? mail.subject : `Re: ${mail.subject}`,
      body: quoteFor(mail),
      inReplyTo: mail.messageId,
      references: [mail.references, mail.messageId].filter(Boolean).join(' '),
      sourceUid: mail.uid,
      sourceFolder: mail.folder,
    };
  };

  const forwardState = (): ComposeState => ({
    to: '',
    cc: '',
    bcc: '',
    subject: /^fwd?:/i.test(mail.subject) ? mail.subject : `Fwd: ${mail.subject}`,
    body: `\n\n---------- ${t('Forwarded message')} ----------\n${t('From')}: ${fmtAddr(mail.fromName, mail.fromAddress)} <${mail.fromAddress}>\n${t('Date')}: ${mail.date ? new Date(mail.date).toLocaleString(locale()) : ''}\n${t('Subject')}: ${mail.subject}\n${t('To')}: ${mail.to}\n\n${mail.body}`,
  });

  const moveTargets = folders.filter((f) => f !== mail.folder);

  return (
    <article className="fs-mail__msg" data-testid="mail-message">
      <header className="fs-mail__msg-bar">
        <IconButton icon={ArrowLeft} label={t('Back to the list')} size="sm" onClick={onBack} />
        <div className="fs-mail__msg-actions">
          <Button variant="secondary" size="sm" icon={Reply} label={t('Reply')} onClick={() => onCompose(replyState(false))} />
          <IconButton icon={ReplyAll} label={t('Reply all')} size="sm" onClick={() => onCompose(replyState(true))} />
          <IconButton icon={Forward} label={t('Forward')} size="sm" onClick={() => onCompose(forwardState())} />
          <span className="fs-mail__sep" />
          <IconButton icon={Archive} label={t('Archive')} size="sm" disabled={busy !== null} onClick={() => void act('archive', () => archiveEmail(mail.uid, mail.folder, accountId), 'gone', t('Archived.'))} />
          <IconButton icon={Trash2} label={t('Delete')} size="sm" disabled={busy !== null} onClick={() => void act('delete', () => deleteEmail(mail.uid, mail.folder, accountId), 'gone', t('Deleted.'))} />
          <IconButton icon={mail.isFlagged ? StarOff : Star} label={mail.isFlagged ? t('Remove the star') : t('Star')} size="sm" disabled={busy !== null} onClick={() => void act('flag', () => flagEmail(mail.uid, !mail.isFlagged, mail.folder, accountId), { isFlagged: !mail.isFlagged }, mail.isFlagged ? t('Unstarred.') : t('Starred.'))} />
          <IconButton icon={Mail} label={t('Mark as unread')} size="sm" disabled={busy !== null} onClick={() => void act('unread', () => markUnread(mail.uid, mail.folder, accountId), { isRead: false }, t('Marked as unread.'))} />
          {moveTargets.length > 0 && (
            <QuickMenu
              label={t('Move to…')}
              icon={Inbox}
              items={moveTargets.map((f) => ({ label: folderLabel(f), onSelect: () => void act('move', () => moveEmail(mail.uid, f, mail.folder, accountId), 'gone', t('Moved to {folder}.', { folder: folderLabel(f) })) }))}
            />
          )}
        </div>
      </header>
      <h2 className="fs-mail__subject">{mail.subject}</h2>
      <div className="fs-mail__meta">
        <span className="fs-mail__from">
          <b>{fmtAddr(mail.fromName, mail.fromAddress)}</b> {mail.fromAddress && mail.fromName && <span className="fs-mail__addr">&lt;{mail.fromAddress}&gt;</span>}
        </span>
        <span className="fs-mail__when">{mail.date ? new Date(mail.date).toLocaleString(locale()) : ''}</span>
        {mail.to && <span className="fs-mail__to">para {mail.to}</span>}
        {mail.cc && <span className="fs-mail__to">cc {mail.cc}</span>}
      </div>
      {mail.attachments.length > 0 && (
        <div className="fs-mail__atts">
          {mail.attachments.map((a) => (
            <a key={a.index} className="fs-mail__att" href={attachmentUrl(mail.uid, a.index, mail.folder, accountId)} target="_blank" rel="noopener">
              <Paperclip size={12} aria-hidden="true" /> {a.filename} {a.size > 0 && <span className="fs-mail__att-size">{bytes(a.size)}</span>}
            </a>
          ))}
        </div>
      )}
      <iframe ref={frameRef} className="fs-mail__frame" title={t('Mail body')} sandbox="allow-popups allow-popups-to-escape-sandbox" srcDoc={html} style={{ blockSize: frameH }} onLoad={onFrameLoad} />
    </article>
  );
}

/* ── Screen ── */

export function EmailScreen() {
  const [accounts, setAccounts] = useState<EmailAccount[] | null>(null);
  const [accountId, setAccountId] = useState<string | null>(null);
  const [folders, setFolders] = useState<string[]>(['INBOX']);
  const [folder, setFolder] = useState('INBOX');
  const [filter, setFilter] = useState<ListFilter>('all');
  const [emails, setEmails] = useState<EmailSummary[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [listError, setListError] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<EmailSummary[] | null>(null);
  const [current, setCurrent] = useState<EmailFull | null>(null);
  const [loadingMail, setLoadingMail] = useState<string | null>(null);
  const [compose, setCompose] = useState<ComposeState | null>(() => takeComposeHandoff());
  const [notice, setNotice] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [refreshing, setRefreshing] = useState(false);

  const say = useCallback((t: string) => {
    setNotice(t);
    window.setTimeout(() => setNotice((c) => (c === t ? null : c)), 4000);
  }, []);

  useEffect(() => {
    const c = new AbortController();
    listAccounts(c.signal)
      .then((list) => {
        setAccounts(list);
        const def = list.find((a) => a.isDefault && a.enabled) ?? list.find((a) => a.enabled) ?? null;
        setAccountId(def?.id ?? null);
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name !== 'AbortError') setFailed(true);
      });
    return () => c.abort();
  }, []);

  useEffect(() => {
    if (accounts === null) return;
    const c = new AbortController();
    listFolders(accountId, c.signal)
      .then((list) => setFolders(list.length ? list : ['INBOX']))
      .catch(() => setFolders(['INBOX']));
    return () => c.abort();
  }, [accounts, accountId]);

  useEffect(() => {
    if (accounts === null) return;
    const c = new AbortController();
    setEmails(null);
    setListError(null);
    listEmails({ folder, accountId, filter, offset, limit: PAGE, refresh: reload > 0 }, c.signal)
      .then((r) => {
        setEmails(r.emails);
        setTotal(r.total);
        setListError(r.error);
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name !== 'AbortError') setListError((err as Error).message || t('Could not read the mail.'));
      })
      .finally(() => setRefreshing(false));
    return () => c.abort();
  }, [accounts, accountId, folder, filter, offset, reload]);

  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) {
      setResults(null);
      return;
    }
    const c = new AbortController();
    const timer = window.setTimeout(() => {
      setSearching(true);
      searchEmails(q, folder, accountId, c.signal)
        .then(setResults)
        .catch(() => setResults([]))
        .finally(() => setSearching(false));
    }, 350);
    return () => {
      window.clearTimeout(timer);
      c.abort();
    };
  }, [query, folder, accountId]);

  const open = async (m: EmailSummary) => {
    setLoadingMail(m.uid);
    try {
      const full = await readEmail(m.uid, m.folder || folder, accountId);
      setCurrent(full);
      setEmails((cur) => (cur ? cur.map((x) => (x.uid === m.uid ? { ...x, isRead: true } : x)) : cur));
    } catch (err) {
      say((err as Error).message || t('Could not open the mail.'));
    } finally {
      setLoadingMail(null);
    }
  };

  const changed = (patch: Partial<EmailSummary> | 'gone') => {
    if (!current) return;
    if (patch === 'gone') {
      setEmails((cur) => (cur ? cur.filter((x) => x.uid !== current.uid) : cur));
      setTotal((t) => Math.max(0, t - 1));
      setCurrent(null);
      return;
    }
    setEmails((cur) => (cur ? cur.map((x) => (x.uid === current.uid ? { ...x, ...patch } : x)) : cur));
    setCurrent({ ...current, ...patch });
    if (patch.isRead === false) setCurrent(null);
  };

  const list = results ?? emails;
  const unread = (emails ?? []).filter((m) => !m.isRead).length;

  if (failed) {
    return (
      <EmptyState
        icon={Mail}
        title={t('Could not read the mail accounts')}
        body={t('The mail endpoint is not responding. The previous interface does not depend on this screen.')}
        primaryAction={{
          label: t('Open the previous interface'),
          onClick: () => {
            window.location.href = '/email?shell=legacy';
          },
        }}
      />
    );
  }

  if (accounts && accounts.length === 0 && !emails?.length && emails !== null && !listError) {
    return (
      <EmptyState
        icon={Mail}
        title={t('No mail accounts')}
        body={t('Accounts (IMAP/SMTP or Google) are set up for now in the previous interface\'s settings.')}
        primaryAction={{
          label: t('Set up in the previous interface'),
          onClick: () => {
            window.location.href = '/email?shell=legacy';
          },
        }}
      />
    );
  }

  return (
    <div className="fs-screen fs-mail" data-testid="email" data-reading={current ? true : undefined}>
      <header className="fs-screen__head fs-mail__head">
        <div>
          <h1 className="fs-screen__title">{t('Mail')}</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {emails ? `${total} en ${folderLabel(folder)}${unread ? ` · ${unread} sin leer en esta página` : ''}.` : t('Read, reply and sort.')}
          </p>
        </div>
        <div className="fs-mail__tools">
          <IconButton
            icon={RefreshCw}
            label={t('Refresh')}
            size="sm"
            disabled={refreshing}
            onClick={() => {
              setRefreshing(true);
              setReload((n) => n + 1);
            }}
          />
          <IconButton
            icon={Settings2}
            label={t('Accounts and rules (previous interface)')}
            size="sm"
            onClick={() => {
              window.location.href = '/email?shell=legacy';
            }}
          />
          <Button variant="primary" size="sm" icon={PenLine} label={t('Compose')} onClick={() => setCompose({ to: '', cc: '', bcc: '', subject: '', body: '' })} testId="mail-compose" />
        </div>
      </header>

      <div className="fs-mail__layout">
        <aside className="fs-mail__side" aria-label={t('Accounts and folders')}>
          {accounts && accounts.length > 1 && (
            <select className="fs-field fs-mail__account" value={accountId ?? ''} onChange={(e) => { setAccountId(e.target.value || null); setOffset(0); setCurrent(null); }} aria-label={t('Account')}>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          )}
          <nav className="fs-mail__folders">
            {folders.map((f) => (
              <button
                key={f}
                type="button"
                className="fs-mail__folder"
                data-on={f === folder || undefined}
                onClick={() => {
                  setFolder(f);
                  setOffset(0);
                  setCurrent(null);
                  setQuery('');
                }}
              >
                {f === 'INBOX' ? <Inbox size={13} aria-hidden="true" /> : <MailOpen size={13} aria-hidden="true" />}
                {folderLabel(f)}
              </button>
            ))}
          </nav>
          <div className="fs-mail__filters" role="group" aria-label={t('Filter')}>
            {(['all', 'unread', 'unanswered', 'favorites'] as ListFilter[]).map((f) => (
              <button key={f} type="button" className="fs-chip" data-on={filter === f || undefined} onClick={() => { setFilter(f); setOffset(0); }}>
                {f === 'all' ? t('All') : f === 'unread' ? t('Unread') : f === 'unanswered' ? t('Unanswered') : t('Starred')}
              </button>
            ))}
          </div>
        </aside>

        <section className="fs-mail__list" aria-label={t('Messages')}>
          <label className="fs-mail__search">
            <Search size={13} aria-hidden="true" />
            <input type="search" placeholder={t('Search in {folder}…', { folder: folderLabel(folder) })} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Search')} />
            {query && <IconButton icon={X} label={t('Clear')} size="sm" onClick={() => setQuery('')} />}
          </label>
          {listError && <p className="fs-mail__error">{listError}</p>}
          {!list && !listError && <Skeleton label={t('Loading messages')} count={6} height="56px" />}
          {searching && <p className="fs-mail__hint">{t('Searching…')}</p>}
          {list && list.length === 0 && !listError && <p className="fs-mail__hint">{results ? t('Nothing matches.') : t('Empty.')}</p>}
          {list && list.length > 0 && (
            <div className="fs-mail__rows">
              {list.map((m) => (
                <button
                  key={`${m.folder}-${m.uid}`}
                  type="button"
                  className="fs-mail__row"
                  data-unread={!m.isRead || undefined}
                  data-current={current?.uid === m.uid || undefined}
                  data-loading={loadingMail === m.uid || undefined}
                  onClick={() => void open(m)}
                  data-testid="mail-row"
                >
                  <span className="fs-mail__row-from">{fmtAddr(m.fromName, m.fromAddress)}</span>
                  <span className="fs-mail__row-when">{fmtWhen(m.date)}</span>
                  <span className="fs-mail__row-subject">
                    {m.isFlagged && <Star size={11} className="fs-mail__star" aria-label={t('Starred')} />}
                    {m.hasAttachments && <Paperclip size={11} aria-label={t('With attachments')} />}
                    {m.subject}
                  </span>
                  {m.snippet && <span className="fs-mail__row-snippet">{m.snippet}</span>}
                </button>
              ))}
            </div>
          )}
          {!results && emails && total > PAGE && (
            <div className="fs-mail__pager">
              <Button variant="ghost" size="sm" label={t('Previous')} disabled={offset === 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE))} />
              <span>
                {offset + 1}–{Math.min(offset + PAGE, total)} de {total}
              </span>
              <Button variant="ghost" size="sm" label={t('Next')} disabled={offset + PAGE >= total} onClick={() => setOffset((o) => o + PAGE)} />
            </div>
          )}
        </section>

        <section className="fs-mail__read" aria-label={t('Message')}>
          {!current && (
            <div className="fs-mail__placeholder">
              <MailOpen size={28} aria-hidden="true" />
              <p>{t('Pick a message.')}</p>
            </div>
          )}
          {current && (
            <Message
              mail={current}
              folders={folders}
              accountId={accountId}
              onBack={() => setCurrent(null)}
              onChanged={changed}
              onCompose={setCompose}
              say={say}
            />
          )}
        </section>
      </div>

      {compose && accounts && (
        <ComposeDialog
          initial={compose}
          accounts={accounts}
          accountId={accountId}
          onClose={() => setCompose(null)}
          onDone={(what) => {
            setCompose(null);
            say(what === 'sent' ? t('Sent.') : t('Draft saved.'));
            if (what === 'sent' && compose.sourceUid && current?.uid === compose.sourceUid) changed({ isAnswered: true });
          }}
          say={say}
        />
      )}

      {notice && (
        <Toast>{notice}</Toast>
      )}
    </div>
  );
}
