import {
  AlertTriangle,
  Archive,
  ArrowLeft,
  Bell,
  BookmarkPlus,
  Check,
  CheckCheck,
  ChevronDown,
  Download,
  FileText,
  Filter,
  FolderInput,
  Forward,
  Image as ImageIcon,
  Keyboard,
  Languages,
  Mail,
  MessageSquare,
  MoreHorizontal,
  Paperclip,
  Reply,
  ReplyAll,
  ShieldOff,
  Sparkles,
  Star,
  StarOff,
  Trash2,
  UserPlus,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { Button, Dialog, IconButton, Menu, Popover, QuickMenu } from '../../components';
import { createSession } from '../../adapters/chat';
import { createNote } from '../../adapters/notes';
import {
  archiveEmail,
  attachmentAsDoc,
  attachmentUrl,
  attachmentsZipUrl,
  deleteEmail,
  flagEmail,
  folderLabel,
  inlineImageUrl,
  listAttachments,
  markDone,
  markNotDone,
  markUnread,
  moveEmail,
  notSpam,
  quoteFor,
  rememberContact,
  summarizeEmail,
  translateEmail,
  type AiTarget,
  type EmailAccount,
  type EmailAttachment,
  type EmailFull,
  type EmailSummary,
  type UrgencyVerdict,
} from '../../adapters/email';
import { bytesLabel, displayName, foldQuotes, foldSignature, parseTurnMeta, sanitizeMailHtml, splitAddresses, splitQuotedText, textToHtml, unquote, visibleTags } from '../../lib/mail';
import { t, locale } from '../../i18n';
import { Avatar, TagChip, fmtFull, useMailKeys } from './parts';
import type { ComposeSeed } from './Compose';

export type Ctx = { folder: string; accountId: string | null };
export type Say = (msg: string, tone?: 'ok' | 'warn') => void;

interface ReaderProps {
  mail: EmailFull;
  account: EmailAccount | null;
  folders: string[];
  ctx: Ctx;
  urgency?: UrgencyVerdict;
  translateLanguage: string;
  onBack: () => void;
  onChanged: (patch: Partial<EmailSummary> | 'gone') => void;
  onCompose: (seed: ComposeSeed) => void;
  onFilterTag: (tag: string) => void;
  onFilterFrom: (address: string) => void;
  onAttachments: (atts: EmailAttachment[]) => void;
  say: Say;
}

const LANGUAGES = ['English', 'Spanish', 'French', 'German', 'Italian', 'Portuguese', 'Catalan', 'Dutch', 'Japanese', 'Chinese'];

function aiTarget(mail: EmailFull, ctx: Ctx): AiTarget {
  return { uid: mail.uid, folder: mail.folder || ctx.folder, accountId: ctx.accountId, messageId: mail.messageId, subject: mail.subject, from: mail.fromName ? `${mail.fromName} <${mail.fromAddress}>` : mail.fromAddress, body: mail.body };
}

/* ── Body ── */

function useRendered(mail: EmailFull, ctx: Ctx, allowRemote: boolean) {
  return useMemo(() => {
    const cid = (c: string) => inlineImageUrl(mail.uid, c, mail.folder || ctx.folder, ctx.accountId);
    const opts = { inlineImageUrl: cid, allowRemoteImages: allowRemote };
    if (mail.threadTurns) {
      let held = 0;
      const turns = mail.threadTurns.map((turn) => {
        const s = sanitizeMailHtml(turn.bodyHtml, opts);
        held += s.heldImages;
        return { level: turn.level, meta: parseTurnMeta(turn.meta), html: turn.level === 0 && mail.senderSignature ? foldSignature(s.html, mail.senderSignature, t('Signature')) : s.html };
      });
      return { turns, html: '', held };
    }
    if (mail.bodyHtml) {
      const s = sanitizeMailHtml(mail.bodyHtml, opts);
      let html = foldQuotes(s.html, t('Earlier messages'));
      if (mail.senderSignature) html = foldSignature(html, mail.senderSignature, t('Signature'));
      return { turns: null, html, held: s.heldImages };
    }
    const { top, quoted } = splitQuotedText(mail.body);
    const html = textToHtml(top) + (quoted ? `<details class="fs-mail__fold" data-kind="quote"><summary>${t('Earlier messages')}</summary><div>${textToHtml(unquote(quoted))}</div></details>` : '');
    return { turns: null, html, held: 0 };
  }, [mail, ctx.folder, ctx.accountId, allowRemote]);
}

function Bubbles({ turns, mail, mine }: { turns: { level: number; meta: { author: string; email: string; date: string }; html: string }[]; mail: EmailFull; mine: Set<string> }) {
  const [showAll, setShowAll] = useState(turns.length <= 3);
  const shown = showAll ? turns : turns.slice(0, 1);
  return (
    <div className="fs-mail__thread">
      {shown.map((turn, i) => {
        const email = turn.level === 0 ? mail.fromAddress.toLowerCase() : turn.meta.email;
        const author = turn.level === 0 ? displayName(mail.fromName, mail.fromAddress) : turn.meta.author || t('Earlier reply');
        const date = turn.level === 0 ? fmtFull(mail.date) : turn.meta.date;
        const isMine = Boolean(email) && mine.has(email);
        return (
          <article key={i} className="fs-mail__bubble" data-mine={isMine || undefined} data-old={turn.level > 0 || undefined}>
            <header className="fs-mail__bubble-head">
              <Avatar name={author} email={email} size="sm" />
              <b>{author}</b>
              {date && <span className="fs-mail__bubble-date">{date}</span>}
            </header>
            <div className="fs-mail__body" dangerouslySetInnerHTML={{ __html: turn.html }} />
          </article>
        );
      })}
      {!showAll && (
        <Button variant="ghost" size="sm" icon={ChevronDown} label={t('Show {n} earlier messages', { n: turns.length - 1 })} onClick={() => setShowAll(true)} />
      )}
    </div>
  );
}

/* ── Panels ── */

function AiPanel({ kind, title, text, busy, model, onClose }: { kind: 'summary' | 'translation'; title: string; text: string | null; busy: boolean; model: string; onClose?: () => void }) {
  const [open, setOpen] = useState(true);
  return (
    <section className="fs-mail__ai" data-kind={kind} aria-live="polite" data-testid={`mail-${kind}`}>
      <header className="fs-mail__ai-head">
        <button type="button" className="fs-mail__ai-toggle" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {kind === 'summary' ? <Sparkles size={12} aria-hidden="true" /> : <Languages size={12} aria-hidden="true" />}
          <span>{title}</span>
          {model && <span className="fs-mail__ai-model">{model}</span>}
          <ChevronDown size={12} aria-hidden="true" data-open={open || undefined} />
        </button>
        {onClose && <IconButton icon={X} label={t('Close')} size="sm" onClick={onClose} />}
      </header>
      {open && <div className="fs-mail__ai-body">{busy ? <span className="fs-mail__ai-busy">{t('Thinking…')}</span> : text}</div>}
    </section>
  );
}

function RemindMenu({ mail, ctx, say }: { mail: EmailFull; ctx: Ctx; say: Say }) {
  const [custom, setCustom] = useState(false);
  const [when, setWhen] = useState('');
  const [note, setNote] = useState<string | null>(null);
  const first = (mail.fromName || mail.fromAddress).replace(/^["']|["']$/g, '').split(/[\s,<]+/)[0] || '';
  const who = first ? first[0].toUpperCase() + first.slice(1) : mail.fromAddress;

  const create = async (due: Date | null, text?: string) => {
    const pad = (n: number) => String(n).padStart(2, '0');
    const iso = due ? `${due.getFullYear()}-${pad(due.getMonth() + 1)}-${pad(due.getDate())}T${pad(due.getHours())}:${pad(due.getMinutes())}` : null;
    const link = `${window.location.origin}/email?folder=${encodeURIComponent(mail.folder || ctx.folder)}&uid=${encodeURIComponent(mail.uid)}`;
    try {
      await createNote({
        title: t('Reply: {subject}', { subject: mail.subject }),
        noteType: 'todo',
        items: [{ text: text || t('Reply to {who}: {subject}', { who, subject: mail.subject }), done: false }],
        content: `${t('Open the mail')}: ${link}`,
        label: 'email reminder',
        dueDate: iso,
      });
      say(due ? t('Reminder set for {when}.', { when: due.toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' }) }) : t('Note saved.'));
    } catch (err) {
      say((err as Error).message || t('Could not create the reminder.'), 'warn');
    }
    setCustom(false);
    setNote(null);
  };

  const later = new Date();
  later.setHours(later.getHours() + 3, 0, 0, 0);
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  tomorrow.setHours(9, 0, 0, 0);
  const nextWeek = new Date();
  nextWeek.setDate(nextWeek.getDate() + ((8 - nextWeek.getDay()) % 7 || 7));
  nextWeek.setHours(9, 0, 0, 0);

  return (
    <>
      <Menu
        trigger={<IconButton icon={Bell} label={t('Remind me to reply')} size="sm" testId="mail-remind" />}
        items={[
          { label: `${t('Later today')} · ${later.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' })}`, onSelect: () => void create(later) },
          { label: `${t('Tomorrow')} · ${tomorrow.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' })}`, onSelect: () => void create(tomorrow) },
          { label: `${t('Next week')} · ${nextWeek.toLocaleDateString(locale(), { weekday: 'short' })}`, onSelect: () => void create(nextWeek) },
          { label: t('Pick a time…'), onSelect: () => setCustom(true) },
          null,
          { label: t('Note without a date…'), onSelect: () => setNote('') },
        ]}
        align="end"
      />
      {custom && (
        <Dialog open onOpenChange={(o) => !o && setCustom(false)} title={t('Remind me to reply')} testId="mail-remind-when" footer={<Button variant="primary" size="sm" label={t('Set the reminder')} disabled={!when} onClick={() => void create(new Date(when))} />}>
          <label className="fs-mail__field">
            <span>{t('When')}</span>
            <input type="datetime-local" className="fs-field" value={when} onChange={(e) => setWhen(e.target.value)} autoFocus />
          </label>
        </Dialog>
      )}
      {note !== null && (
        <Dialog open onOpenChange={(o) => !o && setNote(null)} title={t('Note about this mail')} testId="mail-remind-note" footer={<Button variant="primary" size="sm" label={t('Save the note')} disabled={!note.trim()} onClick={() => void create(null, note.trim())} />}>
          <textarea className="fs-field fs-mail__note" rows={4} value={note} onChange={(e) => setNote(e.target.value)} placeholder={t('Write your note…')} autoFocus />
        </Dialog>
      )}
    </>
  );
}

/* ── Reader ── */

export function Reader({ mail, account, folders, ctx, urgency, translateLanguage, onBack, onChanged, onCompose, onFilterTag, onFilterFrom, onAttachments, say }: ReaderProps) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState<string | null>(null);
  const [view, setView] = useState<'reader' | 'original'>('reader');
  const [allowRemote, setAllowRemote] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const [summary, setSummary] = useState<{ text: string | null; busy: boolean; model: string } | null>(mail.cachedSummary ? { text: mail.cachedSummary, busy: false, model: '' } : null);
  const [translation, setTranslation] = useState<{ text: string | null; busy: boolean; model: string; language: string } | null>(null);
  const [customLang, setCustomLang] = useState<string | null>(null);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [frameH, setFrameH] = useState(320);
  const mailCtx: Ctx = { folder: mail.folder || ctx.folder, accountId: ctx.accountId };

  useEffect(() => {
    setView('reader');
    setAllowRemote(false);
    setShowDetails(false);
    setSummary(mail.cachedSummary ? { text: mail.cachedSummary, busy: false, model: '' } : null);
    setTranslation(null);
  }, [mail.uid, mail.cachedSummary]);

  useEffect(() => {
    if (!mail.attachmentsDeferred) return;
    const c = new AbortController();
    listAttachments(mail.uid, mailCtx.folder, mailCtx.accountId, c.signal)
      .then(onAttachments)
      .catch(() => undefined);
    return () => c.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mail.uid, mail.attachmentsDeferred]);

  const rendered = useRendered(mail, ctx, allowRemote);
  const mine = useMemo(() => new Set(account?.aliases ?? []), [account]);
  const original = useMemo(() => {
    if (view !== 'original') return '';
    const s = sanitizeMailHtml(mail.bodyHtml || textToHtml(mail.body), { keepStyles: true, allowRemoteImages: allowRemote, inlineImageUrl: (c) => inlineImageUrl(mail.uid, c, mailCtx.folder, mailCtx.accountId) });
    return `<!doctype html><html><head><base target="_blank"><style>body{margin:0;padding:12px 14px;font:14px/1.5 system-ui,sans-serif;overflow-wrap:anywhere}img{max-width:100%;height:auto}pre{white-space:pre-wrap}</style></head><body>${s.html}</body></html>`;
  }, [view, mail, allowRemote, mailCtx.folder, mailCtx.accountId]);

  const onFrameLoad = () => {
    try {
      const doc = frameRef.current?.contentDocument;
      if (doc) setFrameH(Math.min(2400, Math.max(160, doc.documentElement.scrollHeight + 8)));
    } catch {
      /* srcdoc is same-origin */
    }
  };

  const act = async (what: string, fn: () => Promise<void>, after: Partial<EmailSummary> | 'gone' | null, okText: string) => {
    setBusy(what);
    try {
      await fn();
      if (after) onChanged(after);
      say(okText);
    } catch (err) {
      say((err as Error).message || t('The operation failed.'), 'warn');
    } finally {
      setBusy(null);
    }
  };

  const replySeed = (all: boolean): ComposeSeed => {
    const me = new Set(account?.aliases ?? []);
    const others = all
      ? [...splitAddresses(mail.to), ...splitAddresses(mail.cc)].filter((a) => a.email && !me.has(a.email) && a.email !== mail.fromAddress.toLowerCase())
      : [];
    return {
      kind: 'reply',
      to: mail.fromName ? `${mail.fromName} <${mail.fromAddress}>` : mail.fromAddress,
      cc: others.map((a) => (a.name ? `${a.name} <${a.email}>` : a.email)).join(', '),
      bcc: '',
      subject: /^re:/i.test(mail.subject) ? mail.subject : `Re: ${mail.subject}`,
      body: quoteFor(mail),
      inReplyTo: mail.messageId,
      references: [mail.references, mail.messageId].filter(Boolean).join(' '),
      sourceUid: mail.uid,
      sourceFolder: mailCtx.folder,
      source: aiTarget(mail, ctx),
      attachments: [],
    };
  };

  const forwardSeed = (): ComposeSeed => ({
    kind: 'forward',
    to: '',
    cc: '',
    bcc: '',
    subject: /^fwd?:/i.test(mail.subject) ? mail.subject : `Fwd: ${mail.subject}`,
    body: `\n\n---------- ${t('Forwarded message')} ----------\n${t('From')}: ${displayName(mail.fromName, mail.fromAddress)} <${mail.fromAddress}>\n${t('Date')}: ${fmtFull(mail.date)}\n${t('Subject')}: ${mail.subject}\n${t('To')}: ${mail.to}\n\n${mail.body}`,
    source: aiTarget(mail, ctx),
    attachments: [],
    forwardFrom: { uid: mail.uid, folder: mailCtx.folder, attachments: mail.attachments.filter((a) => !a.inline) },
  });

  const summarise = async () => {
    setSummary({ text: null, busy: true, model: '' });
    try {
      const r = await summarizeEmail(aiTarget(mail, ctx));
      setSummary({ text: r.summary, busy: false, model: r.model });
    } catch (err) {
      setSummary(null);
      say((err as Error).message || t('Could not summarise.'), 'warn');
    }
  };

  const translate = async (language: string) => {
    setCustomLang(null);
    setTranslation({ text: null, busy: true, model: '', language });
    try {
      const r = await translateEmail(aiTarget(mail, ctx), language);
      setTranslation({ text: r.sameLanguage ? t('The mail is already in {language}.', { language }) : r.translation, busy: false, model: r.model, language });
    } catch (err) {
      setTranslation(null);
      say((err as Error).message || t('Could not translate.'), 'warn');
    }
  };

  const openInChat = async () => {
    setBusy('chat');
    try {
      const id = await createSession(t('Mail: {subject}', { subject: mail.subject.slice(0, 60) }), null);
      const draft = `${t('About this mail from {who} ({subject}):', { who: displayName(mail.fromName, mail.fromAddress), subject: mail.subject })}\n\n${mail.body.slice(0, 6000)}\n\n`;
      navigate(`/studio?s=${encodeURIComponent(id)}&draft=${encodeURIComponent(draft)}`);
    } catch (err) {
      say((err as Error).message || t('Could not open the chat.'), 'warn');
      setBusy(null);
    }
  };

  const saveSender = async () => {
    await rememberContact(displayName(mail.fromName, mail.fromAddress), mail.fromAddress);
    say(t('Saved to contacts.'));
  };

  const asDocument = async (a: EmailAttachment) => {
    setBusy(`doc-${a.index}`);
    try {
      const r = await attachmentAsDoc(mail.uid, a.index, mailCtx.folder, mailCtx.accountId);
      navigate(`/documents/${encodeURIComponent(r.docId)}`);
    } catch (err) {
      say((err as Error).message || t('Could not open the attachment as a document.'), 'warn');
      setBusy(null);
    }
  };

  const keys = useCallback(
    (key: string) => {
      switch (key) {
        case 'r':
          onCompose(replySeed(false));
          return true;
        case 'a':
          onCompose(replySeed(true));
          return true;
        case 'f':
          onCompose(forwardSeed());
          return true;
        case 'Escape':
          onBack();
          return true;
        default:
          return false;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mail, account],
  );
  useMailKeys(keys);

  const files = mail.attachments.filter((a) => !a.inline);
  const tags = visibleTags(mail.tags, mail.isAnswered);
  const moveTargets = folders.filter((f) => f !== mailCtx.folder);
  const done = mail.isAnswered;

  return (
    <article className="fs-mail__msg" data-testid="mail-message" aria-busy={busy !== null || undefined}>
      <header className="fs-mail__msg-bar">
        <IconButton icon={ArrowLeft} label={t('Back to the list')} size="sm" onClick={onBack} />
        <div className="fs-mail__msg-actions">
          <Button variant="secondary" size="sm" icon={Reply} label={t('Reply')} onClick={() => onCompose(replySeed(false))} testId="mail-reply" />
          <IconButton icon={ReplyAll} label={t('Reply all')} size="sm" onClick={() => onCompose(replySeed(true))} />
          <IconButton icon={Forward} label={t('Forward')} size="sm" onClick={() => onCompose(forwardSeed())} />
          <span className="fs-mail__sep" />
          <IconButton
            icon={done ? CheckCheck : Check}
            label={done ? t('Mark as not done') : t('Mark as done')}
            size="sm"
            data-on={done || undefined}
            disabled={busy !== null}
            testId="mail-done"
            onClick={() => void act('done', () => (done ? markNotDone(mail.uid, mailCtx) : markDone(mail.uid, mailCtx)), { isAnswered: !done, isRead: true }, done ? t('Marked as not done.') : t('Done.'))}
          />
          <IconButton icon={mail.isFlagged ? StarOff : Star} label={mail.isFlagged ? t('Remove the star') : t('Star')} size="sm" data-on={mail.isFlagged || undefined} disabled={busy !== null} onClick={() => void act('flag', () => flagEmail(mail.uid, !mail.isFlagged, mailCtx), { isFlagged: !mail.isFlagged }, mail.isFlagged ? t('Unstarred.') : t('Starred.'))} />
          <IconButton icon={Archive} label={t('Archive')} size="sm" disabled={busy !== null} onClick={() => void act('archive', () => archiveEmail(mail.uid, mailCtx), 'gone', t('Archived.'))} />
          <IconButton icon={Trash2} label={t('Delete')} size="sm" disabled={busy !== null} onClick={() => void act('delete', () => deleteEmail(mail.uid, mailCtx), 'gone', t('Deleted.'))} />
          <IconButton icon={Mail} label={t('Mark as unread')} size="sm" disabled={busy !== null} onClick={() => void act('unread', () => markUnread(mail.uid, mailCtx), { isRead: false }, t('Marked as unread.'))} />
          <RemindMenu mail={mail} ctx={ctx} say={say} />
          {moveTargets.length > 0 && (
            <QuickMenu label={t('Move to…')} icon={FolderInput} items={moveTargets.map((f) => ({ label: folderLabel(f), onSelect: () => void act('move', () => moveEmail(mail.uid, f, mailCtx), 'gone', t('Moved to {folder}.', { folder: folderLabel(f) })) }))} />
          )}
          <Menu
            trigger={<IconButton icon={MoreHorizontal} label={t('More')} size="sm" testId="mail-more" />}
            align="end"
            items={[
              { label: t('Summarise with AI'), icon: Sparkles, onSelect: () => void summarise() },
              { label: t('Open in a chat'), icon: MessageSquare, onSelect: () => void openInChat() },
              { label: t('Save sender to contacts'), icon: UserPlus, onSelect: () => void saveSender() },
              { label: t('Only mails from this sender'), icon: Filter, onSelect: () => onFilterFrom(mail.fromAddress) },
              { label: view === 'reader' ? t('Show the original HTML') : t('Show the reader view'), icon: FileText, onSelect: () => setView((v) => (v === 'reader' ? 'original' : 'reader')) },
              null,
              mail.isSpamVerdict ? { label: t('Not spam'), icon: ShieldOff, onSelect: () => void act('spam', () => notSpam(mail.uid), { isSpamVerdict: false }, t('Marked as not spam.')) } : null,
              { label: t('Move to spam'), icon: AlertTriangle, onSelect: () => void act('junk', () => moveEmail(mail.uid, 'Junk', mailCtx), 'gone', t('Moved to spam.')) },
              { label: t('Delete permanently'), icon: Trash2, variant: 'danger', onSelect: () => void act('purge', () => deleteEmail(mail.uid, mailCtx, true), 'gone', t('Deleted permanently.')) },
            ]}
          />
        </div>
      </header>

      <h2 className="fs-mail__subject">{mail.subject}</h2>

      <div className="fs-mail__meta">
        <Avatar name={mail.fromName} email={mail.fromAddress} size="lg" />
        <div className="fs-mail__meta-text">
          <div className="fs-mail__from">
            <b>{displayName(mail.fromName, mail.fromAddress)}</b>
            {mail.fromAddress && <span className="fs-mail__addr">&lt;{mail.fromAddress}&gt;</span>}
            {urgency && (
              <span className="fs-mail__urgency" data-score={urgency.score >= 3 ? 'high' : 'mid'} title={urgency.reason}>
                {urgency.score >= 3 ? t('Urgent') : t('Reply soon')}
              </span>
            )}
          </div>
          <button type="button" className="fs-mail__to-toggle" aria-expanded={showDetails} onClick={() => setShowDetails((s) => !s)}>
            {t('to {who}', { who: splitAddresses(mail.to).map((a) => a.name || a.email).slice(0, 3).join(', ') || t('me') })}
            {mail.cc ? ` · cc ${splitAddresses(mail.cc).length}` : ''} · {fmtFull(mail.date)}
            <ChevronDown size={11} aria-hidden="true" />
          </button>
          {showDetails && (
            <dl className="fs-mail__details">
              <dt>{t('From')}</dt>
              <dd>{mail.fromName ? `${mail.fromName} <${mail.fromAddress}>` : mail.fromAddress}</dd>
              <dt>{t('To')}</dt>
              <dd>{mail.to}</dd>
              {mail.cc && (
                <>
                  <dt>CC</dt>
                  <dd>{mail.cc}</dd>
                </>
              )}
              <dt>{t('Date')}</dt>
              <dd>{fmtFull(mail.date)}</dd>
              {mail.messageId && (
                <>
                  <dt>Message-ID</dt>
                  <dd className="fs-mail__mono">{mail.messageId}</dd>
                </>
              )}
            </dl>
          )}
        </div>
      </div>

      {(tags.length > 0 || mail.isSpamVerdict) && (
        <div className="fs-mail__tags">
          {mail.isSpamVerdict && (
            <span className="fs-mail__tag" data-tag="spam">
              <AlertTriangle size={10} aria-hidden="true" /> {t('Looks like spam')}
            </span>
          )}
          {tags
            .filter((x) => x !== 'spam')
            .map((tag) => (
              <TagChip key={tag} tag={tag} calendarUid={mail.calendarEventUids[0]} onFilter={onFilterTag} />
            ))}
        </div>
      )}

      {summary && <AiPanel kind="summary" title={t('Summary')} text={summary.text} busy={summary.busy} model={summary.model} onClose={() => setSummary(null)} />}
      {translation && <AiPanel kind="translation" title={t('Translated to {language}', { language: translation.language })} text={translation.text} busy={translation.busy} model={translation.model} onClose={() => setTranslation(null)} />}

      <div className="fs-mail__strip">
        {!summary && <Button variant="ghost" size="sm" icon={Sparkles} label={t('Summarise')} onClick={() => void summarise()} testId="mail-summarise" />}
        <Menu
          trigger={<Button variant="ghost" size="sm" icon={Languages} label={t('Translate')} testId="mail-translate" />}
          items={[
            { label: `${translateLanguage} · ${t('default')}`, onSelect: () => void translate(translateLanguage) },
            null,
            ...LANGUAGES.filter((l) => l !== translateLanguage).map((l) => ({ label: l, onSelect: () => void translate(l) })),
            null,
            { label: t('Another language…'), onSelect: () => setCustomLang('') },
          ]}
        />
        {customLang !== null && (
          <form
            className="fs-mail__lang"
            onSubmit={(e) => {
              e.preventDefault();
              if (customLang.trim()) void translate(customLang.trim());
            }}
          >
            <input className="fs-field" value={customLang} onChange={(e) => setCustomLang(e.target.value)} placeholder={t('Language')} autoFocus aria-label={t('Language')} />
            <Button variant="secondary" size="sm" label={t('Translate')} type="submit" />
          </form>
        )}
        <span className="fs-spacer" />
        {rendered.held > 0 && !allowRemote && (
          <Button variant="ghost" size="sm" icon={ImageIcon} label={t('Show {n} remote images', { n: rendered.held })} onClick={() => setAllowRemote(true)} />
        )}
        <span className="fs-mail__keys-btn">
          <Popover trigger={<Button variant="ghost" size="sm" icon={Keyboard} label={t('Keys')} />} align="end">
            <p className="fs-mail__keys-title">{t('Keyboard')}</p>
            <p className="fs-mail__keys">{t('r reply · a reply all · f forward · e archive · # delete · s star · d done · u unread · Esc back')}</p>
          </Popover>
        </span>
      </div>

      {view === 'original' ? (
        <iframe ref={frameRef} className="fs-mail__frame" title={t('Mail body')} sandbox="allow-popups allow-popups-to-escape-sandbox" srcDoc={original} style={{ blockSize: frameH }} onLoad={onFrameLoad} />
      ) : rendered.turns ? (
        <Bubbles turns={rendered.turns} mail={mail} mine={mine} />
      ) : (
        <div className="fs-mail__body fs-mail__body--single" dangerouslySetInnerHTML={{ __html: rendered.html }} />
      )}

      {(files.length > 0 || mail.attachmentsDeferred) && (
        <section className="fs-mail__atts" aria-label={t('Attachments')}>
          <header className="fs-mail__atts-head">
            <Paperclip size={12} aria-hidden="true" />
            <span>{files.length ? t('{n} attachments', { n: files.length }) : t('Loading attachments…')}</span>
            <span className="fs-spacer" />
            {files.length > 1 && (
              <a className="fs-btn" data-variant="ghost" data-size="sm" href={attachmentsZipUrl(mail.uid, mailCtx.folder, mailCtx.accountId)}>
                <Download size={13} aria-hidden="true" /> {t('Download all')}
              </a>
            )}
          </header>
          <ul className="fs-mail__att-list">
            {files.map((a) => (
              <li key={a.index} className="fs-mail__att">
                <a className="fs-mail__att-open" href={attachmentUrl(mail.uid, a.index, mailCtx.folder, mailCtx.accountId)} target="_blank" rel="noopener">
                  <FileText size={14} aria-hidden="true" />
                  <span className="fs-mail__att-name">{a.filename}</span>
                  {a.size > 0 && <span className="fs-mail__att-size">{bytesLabel(a.size)}</span>}
                </a>
                <Menu
                  trigger={<IconButton icon={ChevronDown} label={t('Attachment actions')} size="sm" />}
                  align="end"
                  items={[
                    { label: t('Download'), icon: Download, onSelect: () => window.open(attachmentUrl(mail.uid, a.index, mailCtx.folder, mailCtx.accountId), '_blank', 'noopener') },
                    { label: t('Open as a document'), icon: BookmarkPlus, onSelect: () => void asDocument(a) },
                  ]}
                />
              </li>
            ))}
          </ul>
        </section>
      )}
    </article>
  );
}
