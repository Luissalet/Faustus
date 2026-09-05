import {
  AlertTriangle,
  Archive,
  Check,
  CheckSquare,
  Clock,
  FolderInput,
  Inbox,
  Mail,
  MailOpen,
  MailX,
  Paperclip,
  PenLine,
  Plane,
  RefreshCw,
  Search,
  Settings2,
  Square,
  Star,
  Tag as TagIcon,
  Trash2,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { Button, EmptyState, IconButton, Menu, QuickMenu, Skeleton, Toast } from '../../components';
import {
  archiveEmail,
  awayReplyActive,
  deleteEmail,
  flagEmail,
  folderLabel,
  getMailConfig,
  KNOWN_TAGS,
  listAccounts,
  listEmails,
  listFolders,
  listPendingDrafts,
  listScheduled,
  markDone,
  markRead,
  markUnread,
  moveEmail,
  readEmail,
  searchEmails,
  tagLabel,
  takeComposeHandoffRaw,
  unreadState,
  urgencyByUid,
  type EmailAccount,
  type EmailAttachment,
  type EmailFull,
  type EmailSummary,
  type ListFilter,
  type MailConfig,
  type SearchScope,
  type UrgencyVerdict,
} from '../../adapters/email';
import { displayName, visibleTags } from '../../lib/mail';
import { t, tn } from '../../i18n';
import '../email.css';
import { Compose, type ComposeSeed } from './Compose';
import { MailSettingsDialog } from './MailSettings';
import { Outbox } from './Outbox';
import { Avatar, TagChip, dateBucket, fmtWhen, useMailKeys } from './parts';
import { Reader } from './Reader';
import { UnsubscribeDialog } from './Unsubscribe';

/**
 * Correo. Three columns: where (account, folders, outbox, triage
 * filters and tags), what (the list, with search, bulk actions and
 * single-key shortcuts) and the mail itself (or the composer, which
 * takes the reader's column so the list stays in sight).
 */

const PAGE = 40;

type Tone = 'ok' | 'warn';

const TRIAGE: { key: ListFilter; label: string; icon: typeof Mail }[] = [
  { key: 'all', label: 'All', icon: Inbox },
  { key: 'unread', label: 'Unread', icon: Mail },
  { key: 'undone', label: 'Not done', icon: Square },
  { key: 'favorites', label: 'Starred', icon: Star },
  { key: 'reminders', label: 'Reminders', icon: Clock },
  { key: 'pending_30d', label: 'Pending · 30 days', icon: Clock },
  { key: 'stale_30d', label: 'Older than 30 days', icon: Clock },
];

function filterLabel(f: ListFilter): string {
  const known = TRIAGE.find((x) => x.key === f);
  if (known) return t(known.label);
  if (f.startsWith('tag:')) return tagLabel(f.slice(4));
  return f;
}

function emptySeed(): ComposeSeed {
  return { kind: 'new', to: '', cc: '', bcc: '', subject: '', body: '', attachments: [] };
}

function takeHandoff(): ComposeSeed | null {
  const d = takeComposeHandoffRaw();
  if (!d) return null;
  return {
    kind: d.inReplyTo ? 'reply' : 'new',
    to: d.to ?? '',
    cc: d.cc ?? '',
    bcc: d.bcc ?? '',
    subject: d.subject ?? '',
    body: d.body ?? '',
    inReplyTo: d.inReplyTo,
    references: d.references,
    attachments: d.attachmentToken ? [{ token: d.attachmentToken, filename: d.attachmentName ?? 'attachment', size: 0 }] : [],
  };
}

export function EmailScreen() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [accounts, setAccounts] = useState<EmailAccount[] | null>(null);
  const [accountId, setAccountId] = useState<string | null>(null);
  const [folders, setFolders] = useState<string[]>(['INBOX']);
  const [folder, setFolder] = useState(params.get('folder') || 'INBOX');
  const [filter, setFilter] = useState<ListFilter>((params.get('filter') as ListFilter) || 'all');
  const [withFiles, setWithFiles] = useState(false);
  const [fromFilter, setFromFilter] = useState<string | null>(null);
  const [emails, setEmails] = useState<EmailSummary[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [listError, setListError] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState('');
  const [scope, setScope] = useState<SearchScope>('current');
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<EmailSummary[] | null>(null);
  const [current, setCurrent] = useState<EmailFull | null>(null);
  const [loadingMail, setLoadingMail] = useState<string | null>(null);
  const [compose, setCompose] = useState<ComposeSeed | null>(() => takeHandoff());
  const [view, setView] = useState<'mail' | 'outbox'>('mail');
  const [outboxCounts, setOutboxCounts] = useState({ pending: 0, scheduled: 0 });
  const [unread, setUnread] = useState<number | null>(null);
  const [urgency, setUrgency] = useState<Map<string, UrgencyVerdict>>(new Map());
  const [cfg, setCfg] = useState<MailConfig | null>(null);
  const [notice, setNotice] = useState<{ text: string; tone: Tone } | null>(null);
  const [reload, setReload] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [cursor, setCursor] = useState(0);
  const [dialog, setDialog] = useState<'unsubscribe' | 'settings' | null>(null);
  const noticeTimer = useRef<number | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const pendingOpen = useRef<{ uid: string; folder: string } | null>(params.get('uid') ? { uid: params.get('uid') as string, folder: params.get('folder') || 'INBOX' } : null);

  const say = useCallback((text: string, tone: Tone = 'ok') => {
    setNotice({ text, tone });
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), tone === 'warn' ? 7000 : 4000);
  }, []);

  const ctx = useMemo(() => ({ folder, accountId }), [folder, accountId]);
  const account = accounts?.find((a) => a.id === accountId) ?? null;

  /* Legacy deep links: /#email=folder:uid */
  useEffect(() => {
    const m = window.location.hash.match(/^#email=([^:]+):(.+)$/);
    if (m) {
      pendingOpen.current = { uid: decodeURIComponent(m[2]), folder: decodeURIComponent(m[1]) };
      history.replaceState(null, '', window.location.pathname + window.location.search);
    }
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
    getMailConfig(accountId)
      .then(setCfg)
      .catch(() => setCfg(null));
    urgencyByUid(c.signal).then(setUrgency);
    return () => c.abort();
  }, [accounts, accountId]);

  useEffect(() => {
    if (accounts === null) return;
    const c = new AbortController();
    setEmails(null);
    setListError(null);
    setCursor(0);
    listEmails({ folder, accountId, filter, offset, limit: PAGE, from: fromFilter ?? undefined, hasAttachments: withFiles, refresh: reload > 0 }, c.signal)
      .then((r) => {
        setEmails(r.emails);
        setTotal(r.total);
        setListError(r.error);
        if (reload > 0) urgencyByUid().then(setUrgency);
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name !== 'AbortError') setListError((err as Error).message || t('Could not read the mail.'));
      })
      .finally(() => setRefreshing(false));
    return () => c.abort();
  }, [accounts, accountId, folder, filter, offset, reload, fromFilter, withFiles]);

  /* Outbox badge: what waits for approval and what is scheduled. */
  useEffect(() => {
    if (accounts === null) return;
    const c = new AbortController();
    Promise.all([listPendingDrafts(c.signal), listScheduled(c.signal)])
      .then(([p, s]) => setOutboxCounts({ pending: p.length, scheduled: s.filter((x) => x.status === 'pending').length }))
      .catch(() => undefined);
    return () => c.abort();
  }, [accounts, reload]);

  /* Unread count for the rail, kept fresh while the screen is open. */
  useEffect(() => {
    if (accounts === null || !accounts.length) return;
    let stop = false;
    const c = new AbortController();
    const tick = () => {
      unreadState('INBOX', accountId, c.signal)
        .then((s) => {
          if (!stop) setUnread(s.unreadCount);
        })
        .catch(() => undefined);
    };
    tick();
    const timer = window.setInterval(tick, 60000);
    return () => {
      stop = true;
      c.abort();
      window.clearInterval(timer);
    };
  }, [accounts, accountId, reload]);

  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) {
      setResults(null);
      return;
    }
    const c = new AbortController();
    const timer = window.setTimeout(() => {
      setSearching(true);
      searchEmails(q, folder, accountId, scope, c.signal)
        .then(setResults)
        .catch(() => setResults([]))
        .finally(() => setSearching(false));
    }, 350);
    return () => {
      window.clearTimeout(timer);
      c.abort();
    };
  }, [query, folder, accountId, scope]);

  const open = useCallback(
    async (m: Pick<EmailSummary, 'uid' | 'folder'>) => {
      setLoadingMail(m.uid);
      try {
        const full = await readEmail(m.uid, m.folder || folder, accountId);
        setCurrent(full);
        setCompose(null);
        setView('mail');
        setEmails((cur) => (cur ? cur.map((x) => (x.uid === m.uid ? { ...x, isRead: true } : x)) : cur));
        setUnread((u) => (u === null ? u : Math.max(0, u - 1)));
      } catch (err) {
        say((err as Error).message || t('Could not open the mail.'), 'warn');
      } finally {
        setLoadingMail(null);
      }
    },
    [folder, accountId, say],
  );

  /* ?uid=…&folder=… (notes' reminders, the legacy hash). */
  useEffect(() => {
    if (accounts === null || !pendingOpen.current) return;
    const target = pendingOpen.current;
    pendingOpen.current = null;
    if (target.folder !== folder) setFolder(target.folder);
    void open({ uid: target.uid, folder: target.folder });
    setParams((p) => {
      p.delete('uid');
      p.delete('folder');
      return p;
    }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accounts]);

  const changed = (patch: Partial<EmailSummary> | 'gone') => {
    if (!current) return;
    if (patch === 'gone') {
      setEmails((cur) => (cur ? cur.filter((x) => x.uid !== current.uid) : cur));
      setTotal((n) => Math.max(0, n - 1));
      setCurrent(null);
      return;
    }
    setEmails((cur) => (cur ? cur.map((x) => (x.uid === current.uid ? { ...x, ...patch } : x)) : cur));
    setCurrent({ ...current, ...patch });
    if (patch.isRead === false) {
      setCurrent(null);
      setUnread((u) => (u === null ? u : u + 1));
    }
  };

  const setAttachments = (atts: EmailAttachment[]) => setCurrent((c) => (c ? { ...c, attachments: [...atts, ...c.attachments.filter((a) => a.inline)], attachmentsDeferred: false } : c));

  const goFolder = (f: string) => {
    setFolder(f);
    setOffset(0);
    setCurrent(null);
    setQuery('');
    setFromFilter(null);
    setView('mail');
    setSelected(new Set());
  };

  const goFilter = (f: ListFilter) => {
    setFilter((cur) => (cur === f && f !== 'all' ? 'all' : f));
    setOffset(0);
    setView('mail');
    setSelected(new Set());
  };

  const refresh = () => {
    setRefreshing(true);
    setReload((n) => n + 1);
  };

  /* ── List actions (row quick actions, bulk, keys) ── */

  const rowAct = async (m: EmailSummary, what: 'archive' | 'delete' | 'star' | 'done' | 'read' | 'unread', okText: string) => {
    const c = { folder: m.folder || folder, accountId };
    try {
      if (what === 'archive') await archiveEmail(m.uid, c);
      else if (what === 'delete') await deleteEmail(m.uid, c);
      else if (what === 'star') await flagEmail(m.uid, !m.isFlagged, c);
      else if (what === 'done') await markDone(m.uid, c);
      else if (what === 'read') await markRead(m.uid, c);
      else await markUnread(m.uid, c);
      if (what === 'archive' || what === 'delete') {
        setEmails((cur) => (cur ? cur.filter((x) => x.uid !== m.uid) : cur));
        setTotal((n) => Math.max(0, n - 1));
        if (current?.uid === m.uid) setCurrent(null);
      } else {
        const patch: Partial<EmailSummary> = what === 'star' ? { isFlagged: !m.isFlagged } : what === 'done' ? { isAnswered: true, isRead: true } : { isRead: what === 'read' };
        setEmails((cur) => (cur ? cur.map((x) => (x.uid === m.uid ? { ...x, ...patch } : x)) : cur));
        if (current?.uid === m.uid) setCurrent({ ...current, ...patch });
      }
      say(okText);
    } catch (err) {
      say((err as Error).message || t('The operation failed.'), 'warn');
    }
  };

  const bulk = async (what: 'archive' | 'delete' | 'done' | 'read' | 'unread' | 'move', dest?: string) => {
    const list = (results ?? emails ?? []).filter((m) => selected.has(m.uid));
    if (!list.length) return;
    let okCount = 0;
    for (const m of list) {
      const c = { folder: m.folder || folder, accountId };
      try {
        if (what === 'archive') await archiveEmail(m.uid, c);
        else if (what === 'delete') await deleteEmail(m.uid, c);
        else if (what === 'done') await markDone(m.uid, c);
        else if (what === 'read') await markRead(m.uid, c);
        else if (what === 'unread') await markUnread(m.uid, c);
        else if (dest) await moveEmail(m.uid, dest, c);
        okCount += 1;
      } catch {
        /* counted below */
      }
    }
    say(okCount === list.length ? tn(okCount, '{n} mail updated.', '{n} mails updated.') : t('{ok} of {n} done; the rest failed.', { ok: okCount, n: list.length }), okCount === list.length ? 'ok' : 'warn');
    setSelected(new Set());
    setSelecting(false);
    refresh();
  };

  const list = results ?? emails;
  const ordered = useMemo(() => {
    if (!list) return null;
    if (results || filter !== 'all') return list;
    return [...list.filter((m) => m.isFlagged), ...list.filter((m) => !m.isFlagged)];
  }, [list, results, filter]);

  const keys = useCallback(
    (key: string) => {
      if (!ordered || !ordered.length) {
        if (key === 'c') {
          setCompose(emptySeed());
          setCurrent(null);
          return true;
        }
        if (key === '/') {
          searchRef.current?.focus();
          return true;
        }
        return false;
      }
      const at = Math.min(cursor, ordered.length - 1);
      const m = ordered[at];
      switch (key) {
        case 'j':
        case 'ArrowDown':
          setCursor(Math.min(ordered.length - 1, at + 1));
          if (current) void open(ordered[Math.min(ordered.length - 1, at + 1)]);
          return true;
        case 'k':
        case 'ArrowUp':
          setCursor(Math.max(0, at - 1));
          if (current) void open(ordered[Math.max(0, at - 1)]);
          return true;
        case 'Enter':
        case 'o':
          void open(m);
          return true;
        case 'e':
          void rowAct(m, 'archive', t('Archived.'));
          return true;
        case '#':
          void rowAct(m, 'delete', t('Deleted.'));
          return true;
        case 's':
          void rowAct(m, 'star', m.isFlagged ? t('Unstarred.') : t('Starred.'));
          return true;
        case 'd':
          void rowAct(m, 'done', t('Done.'));
          return true;
        case 'u':
          void rowAct(m, 'unread', t('Marked as unread.'));
          return true;
        case 'c':
          setCompose(emptySeed());
          setCurrent(null);
          return true;
        case 'x':
          setSelecting(true);
          setSelected((s) => {
            const n = new Set(s);
            if (n.has(m.uid)) n.delete(m.uid);
            else n.add(m.uid);
            return n;
          });
          return true;
        case '/':
          searchRef.current?.focus();
          return true;
        default:
          return false;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ordered, cursor, current, open],
  );
  useMailKeys(keys, !compose);

  /* ── Empty and failed states ── */

  if (failed) {
    return (
      <EmptyState icon={Mail} title={t('Could not read the mail accounts')} body={t('The mail endpoint is not responding. Check the server log, or the account in Settings → Integrations.')} primaryAction={{ label: t('Open Settings'), onClick: () => navigate('/settings?s=integrations') }} />
    );
  }

  if (accounts && accounts.length === 0 && emails !== null && !emails.length && !listError) {
    return (
      <EmptyState icon={Mail} title={t('No mail accounts')} body={t('Add an IMAP/SMTP or Google account in Settings → Integrations and the inbox appears here.')} primaryAction={{ label: t('Add an account'), onClick: () => navigate('/settings?s=integrations') }} />
    );
  }

  const unreadHere = (emails ?? []).filter((m) => !m.isRead).length;
  const reading = Boolean(current || compose || view === 'outbox');
  const groups: { label: string; items: EmailSummary[] }[] = [];
  if (ordered) {
    for (const m of ordered) {
      const label = m.isFlagged && !results && filter === 'all' ? t('Starred') : dateBucket(m.date);
      const g = groups[groups.length - 1];
      if (g && g.label === label) g.items.push(m);
      else groups.push({ label, items: [m] });
    }
  }
  const allSelected = ordered ? ordered.length > 0 && ordered.every((m) => selected.has(m.uid)) : false;

  return (
    <div className="fs-screen fs-mail" data-testid="email" data-reading={reading || undefined} data-composing={compose ? true : undefined}>
      <header className="fs-screen__head fs-mail__head">
        <div>
          <h1 className="fs-screen__title">{t('Mail')}</h1>
          <p className="fs-prose fs-mail__lede">
            {emails ? (
              <>
                {tn(total, '{n} in {folder}', '{n} in {folder}', { folder: folderLabel(folder) })}
                {unreadHere ? ` · ${tn(unreadHere, '{n} unread on this page', '{n} unread on this page')}` : ''}
                {filter !== 'all' ? ` · ${filterLabel(filter)}` : ''}
              </>
            ) : (
              t('Read, reply and sort.')
            )}
          </p>
        </div>
        <div className="fs-mail__tools">
          <IconButton icon={RefreshCw} label={t('Refresh')} size="sm" disabled={refreshing} onClick={refresh} testId="mail-refresh" />
          <IconButton icon={MailX} label={t('Unsubscribe review')} size="sm" onClick={() => setDialog('unsubscribe')} testId="mail-unsub" />
          <IconButton icon={Settings2} label={t('Mail settings')} size="sm" onClick={() => setDialog('settings')} testId="mail-settings-open" />
          <Button
            variant="primary"
            size="sm"
            icon={PenLine}
            label={t('Compose')}
            onClick={() => {
              setCompose(emptySeed());
              setCurrent(null);
              setView('mail');
            }}
            testId="mail-compose"
          />
        </div>
      </header>

      {awayReplyActive(cfg) && (
        <p className="fs-notice fs-mail__away" data-tone="warning">
          <Plane size={13} aria-hidden="true" />
          {cfg?.autoReplyEnd ? t('The away reply is on until {date}.', { date: cfg.autoReplyEnd }) : t('The away reply is on.')}
          <button type="button" className="fs-mail__link" onClick={() => setDialog('settings')}>
            {t('Change')}
          </button>
        </p>
      )}

      <div className="fs-mail__layout">
        <aside className="fs-mail__side" aria-label={t('Accounts and folders')}>
          {accounts && accounts.length > 1 && (
            <select
              className="fs-field fs-mail__account"
              value={accountId ?? ''}
              onChange={(e) => {
                setAccountId(e.target.value || null);
                setOffset(0);
                setCurrent(null);
                setSelected(new Set());
              }}
              aria-label={t('Account')}
            >
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          )}
          <nav className="fs-mail__folders" aria-label={t('Folders')}>
            {folders.map((f) => (
              <button key={f} type="button" className="fs-mail__folder" data-on={(f === folder && view === 'mail') || undefined} onClick={() => goFolder(f)}>
                {f === 'INBOX' ? <Inbox size={13} aria-hidden="true" /> : <MailOpen size={13} aria-hidden="true" />}
                <span className="fs-mail__folder-name">{folderLabel(f)}</span>
                {f === 'INBOX' && unread ? <span className="fs-mail__count">{unread}</span> : null}
              </button>
            ))}
            <button
              type="button"
              className="fs-mail__folder"
              data-on={view === 'outbox' || undefined}
              onClick={() => {
                setView('outbox');
                setCurrent(null);
                setCompose(null);
              }}
              data-testid="mail-outbox-nav"
            >
              <Clock size={13} aria-hidden="true" />
              <span className="fs-mail__folder-name">{t('Outbox')}</span>
              {outboxCounts.pending + outboxCounts.scheduled > 0 && <span className="fs-mail__count" data-attention={outboxCounts.pending > 0 || undefined}>{outboxCounts.pending + outboxCounts.scheduled}</span>}
            </button>
          </nav>

          <div className="fs-mail__rail-group" role="group" aria-label={t('Triage')}>
            <span className="fs-mail__rail-title">{t('Triage')}</span>
            {TRIAGE.map((f) => (
              <button key={f.key} type="button" className="fs-mail__folder" data-on={(filter === f.key && view === 'mail') || undefined} onClick={() => goFilter(f.key)} data-testid={`mail-filter-${f.key}`}>
                <f.icon size={13} aria-hidden="true" />
                <span className="fs-mail__folder-name">{t(f.label)}</span>
              </button>
            ))}
            <button type="button" className="fs-mail__folder" data-on={withFiles || undefined} onClick={() => { setWithFiles((v) => !v); setOffset(0); }}>
              <Paperclip size={13} aria-hidden="true" />
              <span className="fs-mail__folder-name">{t('With attachments')}</span>
            </button>
          </div>

          <div className="fs-mail__rail-group" role="group" aria-label={t('Tags')}>
            <span className="fs-mail__rail-title">{t('Tags')}</span>
            <div className="fs-mail__rail-tags">
              {KNOWN_TAGS.filter((k) => k.tag !== 'calendar').map((k) => (
                <button key={k.tag} type="button" className="fs-mail__tag" data-tag={k.tag} data-on={filter === `tag:${k.tag}` || undefined} onClick={() => goFilter(`tag:${k.tag}`)}>
                  <TagIcon size={10} aria-hidden="true" /> {t(k.label)}
                </button>
              ))}
            </div>
          </div>
        </aside>

        <section className="fs-mail__list" aria-label={t('Messages')}>
          {view === 'outbox' ? (
            <Outbox say={say} onCounts={(pending, scheduled) => setOutboxCounts({ pending, scheduled })} />
          ) : (
            <>
              <div className="fs-mail__searchbar">
                <label className="fs-mail__search">
                  <Search size={13} aria-hidden="true" />
                  <input ref={searchRef} type="search" placeholder={t('Search…')} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Search')} data-testid="mail-search" />
                  {query && <IconButton icon={X} label={t('Clear')} size="sm" onClick={() => setQuery('')} />}
                </label>
                <select className="fs-field fs-mail__scope" value={scope} onChange={(e) => setScope(e.target.value as SearchScope)} aria-label={t('Where to search')}>
                  <option value="current">{folderLabel(folder)}</option>
                  <option value="inbox">{t('Inbox')}</option>
                  <option value="sent">{t('Sent')}</option>
                  <option value="all">{t('Everywhere')}</option>
                </select>
                <IconButton icon={selecting ? CheckSquare : Square} label={selecting ? t('Stop selecting') : t('Select')} size="sm" data-on={selecting || undefined} onClick={() => { setSelecting((s) => !s); setSelected(new Set()); }} testId="mail-select" />
              </div>

              {(fromFilter || withFiles || (filter !== 'all' && !TRIAGE.some((x) => x.key === filter))) && (
                <div className="fs-mail__active">
                  {fromFilter && (
                    <button type="button" className="fs-chip" data-on onClick={() => setFromFilter(null)}>
                      {t('From {who}', { who: fromFilter })} <X size={11} aria-hidden="true" />
                    </button>
                  )}
                  {filter.startsWith('tag:') && (
                    <button type="button" className="fs-chip" data-on onClick={() => goFilter('all')}>
                      {filterLabel(filter)} <X size={11} aria-hidden="true" />
                    </button>
                  )}
                  {withFiles && (
                    <button type="button" className="fs-chip" data-on onClick={() => setWithFiles(false)}>
                      {t('With attachments')} <X size={11} aria-hidden="true" />
                    </button>
                  )}
                </div>
              )}

              {selecting && ordered && ordered.length > 0 && (
                <div className="fs-mail__bulk" data-testid="mail-bulk">
                  <label className="fs-switch">
                    <input type="checkbox" checked={allSelected} onChange={(e) => setSelected(e.target.checked ? new Set(ordered.map((m) => m.uid)) : new Set())} />
                    <span>{selected.size ? tn(selected.size, '{n} selected', '{n} selected') : t('Select all')}</span>
                  </label>
                  <span className="fs-spacer" />
                  <IconButton icon={Check} label={t('Done')} size="sm" disabled={!selected.size} onClick={() => void bulk('done')} />
                  <IconButton icon={MailOpen} label={t('Mark as read')} size="sm" disabled={!selected.size} onClick={() => void bulk('read')} />
                  <IconButton icon={Mail} label={t('Mark as unread')} size="sm" disabled={!selected.size} onClick={() => void bulk('unread')} />
                  <IconButton icon={Archive} label={t('Archive')} size="sm" disabled={!selected.size} onClick={() => void bulk('archive')} />
                  {folders.length > 1 && <QuickMenu label={t('Move to…')} icon={FolderInput} items={folders.filter((f) => f !== folder).map((f) => ({ label: folderLabel(f), onSelect: () => void bulk('move', f) }))} />}
                  <IconButton icon={Trash2} label={t('Delete')} size="sm" disabled={!selected.size} onClick={() => void bulk('delete')} />
                </div>
              )}

              {listError && <p className="fs-mail__error">{listError}</p>}
              {!ordered && !listError && <Skeleton label={t('Loading messages')} count={7} height="60px" />}
              {searching && <p className="fs-mail__hint">{t('Searching…')}</p>}
              {ordered && ordered.length === 0 && !listError && (
                <p className="fs-mail__hint">{results ? t('Nothing matches.') : filter === 'unread' ? t('All read.') : filter === 'undone' ? t('All done.') : t('Empty.')}</p>
              )}

              {groups.map((g) => (
                <div key={g.label} className="fs-mail__group-block">
                  <h3 className="fs-mail__group">{g.label}</h3>
                  <ul className="fs-mail__rows">
                    {g.items.map((m) => {
                      const idx = ordered ? ordered.indexOf(m) : -1;
                      const urg = urgency.get(m.uid);
                      const tags = visibleTags(m.tags, m.isAnswered);
                      return (
                        <li
                          key={`${m.folder}-${m.uid}`}
                          className="fs-mail__row"
                          data-unread={!m.isRead || undefined}
                          data-done={m.isAnswered || undefined}
                          data-current={current?.uid === m.uid || undefined}
                          data-cursor={idx === cursor || undefined}
                          data-loading={loadingMail === m.uid || undefined}
                          data-urgency={urg ? (urg.score >= 3 ? 'high' : 'mid') : undefined}
                          data-selected={selected.has(m.uid) || undefined}
                          data-testid="mail-row"
                        >
                          {selecting && (
                            <label className="fs-mail__row-check">
                              <input
                                type="checkbox"
                                checked={selected.has(m.uid)}
                                aria-label={t('Select {subject}', { subject: m.subject })}
                                onChange={(e) =>
                                  setSelected((s) => {
                                    const n = new Set(s);
                                    if (e.target.checked) n.add(m.uid);
                                    else n.delete(m.uid);
                                    return n;
                                  })
                                }
                              />
                            </label>
                          )}
                          <button
                            type="button"
                            className="fs-mail__row-main"
                            title={urg?.reason || undefined}
                            onClick={() => {
                              setCursor(idx);
                              void open(m);
                            }}
                          >
                            <Avatar name={m.fromName} email={m.fromAddress} size="md" />
                            <span className="fs-mail__row-text">
                              <span className="fs-mail__row-top">
                                <span className="fs-mail__row-from">{displayName(m.fromName, m.fromAddress)}</span>
                                <span className="fs-mail__row-when">{fmtWhen(m.date)}</span>
                              </span>
                              <span className="fs-mail__row-subject">
                                {m.isFlagged && <Star size={11} className="fs-mail__star" aria-label={t('Starred')} />}
                                {m.isAnswered && <Check size={11} className="fs-mail__done" aria-label={t('Done')} />}
                                {m.hasAttachments && <Paperclip size={11} aria-label={t('With attachments')} />}
                                {m.subject}
                              </span>
                              {(m.snippet || tags.length > 0) && (
                                <span className="fs-mail__row-bottom">
                                  {m.snippet && <span className="fs-mail__row-snippet">{m.snippet}</span>}
                                  {tags.length > 0 && (
                                    <span className="fs-mail__row-tags">
                                      {tags.slice(0, 2).map((tag) => (
                                        <span key={tag} className="fs-mail__tag" data-tag={tag}>
                                          {tagLabel(tag)}
                                        </span>
                                      ))}
                                      {tags.length > 2 && <span className="fs-mail__tag">+{tags.length - 2}</span>}
                                    </span>
                                  )}
                                </span>
                              )}
                            </span>
                          </button>
                          {!selecting && (
                            <span className="fs-mail__row-quick">
                              <IconButton icon={Archive} label={t('Archive')} size="sm" onClick={() => void rowAct(m, 'archive', t('Archived.'))} />
                              <IconButton icon={Trash2} label={t('Delete')} size="sm" onClick={() => void rowAct(m, 'delete', t('Deleted.'))} />
                              <IconButton icon={Star} label={m.isFlagged ? t('Remove the star') : t('Star')} size="sm" data-on={m.isFlagged || undefined} onClick={() => void rowAct(m, 'star', m.isFlagged ? t('Unstarred.') : t('Starred.'))} />
                              <IconButton icon={Check} label={t('Done')} size="sm" data-on={m.isAnswered || undefined} onClick={() => void rowAct(m, 'done', t('Done.'))} />
                            </span>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ))}

              {!results && emails && total > PAGE && (
                <div className="fs-mail__pager">
                  <Button variant="ghost" size="sm" label={t('Previous')} disabled={offset === 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE))} />
                  <span>{t('{from}–{to} of {total}', { from: offset + 1, to: Math.min(offset + PAGE, total), total })}</span>
                  <Button variant="ghost" size="sm" label={t('Next')} disabled={offset + PAGE >= total} onClick={() => setOffset((o) => o + PAGE)} />
                </div>
              )}
            </>
          )}
        </section>

        <section className="fs-mail__read" aria-label={compose ? t('Composer') : t('Message')}>
          {compose && accounts && (
            <Compose
              seed={compose}
              accounts={accounts}
              accountId={accountId}
              onClose={() => setCompose(null)}
              onDone={(what, seed) => {
                setCompose(null);
                if (what === 'sent') say(t('Sent.'));
                else if (what === 'draft') say(t('Draft saved.'));
                if (what === 'sent' && seed.sourceUid) {
                  setEmails((cur) => (cur ? cur.map((x) => (x.uid === seed.sourceUid ? { ...x, isAnswered: true } : x)) : cur));
                  if (current?.uid === seed.sourceUid) setCurrent({ ...current, isAnswered: true });
                }
                if (what === 'scheduled') setOutboxCounts((c) => ({ ...c, scheduled: c.scheduled + 1 }));
              }}
              say={say}
            />
          )}
          {!compose && !current && (
            <div className="fs-mail__placeholder">
              <MailOpen size={28} aria-hidden="true" />
              <p>{t('Pick a message.')}</p>
              <p className="fs-mail__keys">{t('j/k move · Enter opens · e archives · # deletes · s stars · d done · c composes · / searches')}</p>
            </div>
          )}
          {!compose && current && (
            <Reader
              mail={current}
              account={account}
              folders={folders}
              ctx={ctx}
              urgency={urgency.get(current.uid)}
              translateLanguage={cfg?.translateLanguage || 'English'}
              onBack={() => setCurrent(null)}
              onChanged={changed}
              onCompose={(seed) => setCompose(seed)}
              onFilterTag={(tag) => goFilter(`tag:${tag}`)}
              onFilterFrom={(address) => {
                setFromFilter(address);
                setOffset(0);
                setCurrent(null);
              }}
              onAttachments={setAttachments}
              say={say}
            />
          )}
        </section>
      </div>

      {dialog === 'unsubscribe' && <UnsubscribeDialog folder={folder} accountId={accountId} onClose={() => setDialog(null)} say={say} onChanged={refresh} />}
      {dialog === 'settings' && accounts && <MailSettingsDialog accounts={accounts} accountId={accountId} onClose={() => setDialog(null)} onSaved={setCfg} say={say} />}

      {notice && (
        <Toast>
          {notice.tone === 'warn' ? <AlertTriangle size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />} {notice.text}
        </Toast>
      )}
    </div>
  );
}
