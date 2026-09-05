import { Clock, FileText, Image as ImageIcon, Library, Paperclip, Send, Sparkles, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Dialog, IconButton, Menu, Popover, Skeleton } from '../../components';
import { loadDocLibrary, type LibraryDoc } from '../../adapters/documents';
import { loadGallery, type GalleryImage } from '../../adapters/gallery';
import {
  aiReply,
  attachFromLibrary,
  attachFromMail,
  attachManyAsZip,
  discardAttachment,
  rememberContact,
  saveDraft,
  scheduleEmail,
  searchContacts,
  sendEmail,
  uploadAttachment,
  type AiTarget,
  type ContactHit,
  type EmailAccount,
  type EmailAttachment,
  type Outgoing,
  type StagedAttachment,
} from '../../adapters/email';
import { bytesLabel, formatAddress, isValidEmail, joinAddresses, mentionsAttachment, parseAddress, splitAddresses, type Address } from '../../lib/mail';
import { t, locale } from '../../i18n';
import { Avatar } from './parts';

export interface ComposeSeed {
  kind: 'new' | 'reply' | 'forward';
  to: string;
  cc: string;
  bcc: string;
  subject: string;
  body: string;
  inReplyTo?: string;
  references?: string;
  sourceUid?: string;
  sourceFolder?: string;
  /** The mail being answered, for the AI draft. */
  source?: AiTarget;
  attachments: StagedAttachment[];
  /** Forwarding re-stages the original's files on the server. */
  forwardFrom?: { uid: string; folder: string; attachments: EmailAttachment[] };
}

export type ComposeOutcome = 'sent' | 'draft' | 'scheduled';

interface ComposeProps {
  seed: ComposeSeed;
  accounts: EmailAccount[];
  accountId: string | null;
  onClose: () => void;
  onDone: (what: ComposeOutcome, seed: ComposeSeed) => void;
  say: (msg: string, tone?: 'ok' | 'warn') => void;
}

/* ── Recipients with chips and contact search ── */

function Recipients({ label, value, onChange, autoFocus, testId }: { label: string; value: string; onChange: (v: string) => void; autoFocus?: boolean; testId?: string }) {
  const chips = useMemo(() => splitAddresses(value), [value]);
  const [typing, setTyping] = useState('');
  const [hits, setHits] = useState<ContactHit[]>([]);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const q = typing.trim();
    if (q.length < 2) {
      setHits([]);
      return;
    }
    const c = new AbortController();
    const timer = window.setTimeout(() => {
      searchContacts(q, c.signal)
        .then((r) => {
          setHits(r.filter((h) => !chips.some((x) => x.email === h.email.toLowerCase())));
          setActive(0);
        })
        .catch(() => setHits([]));
    }, 200);
    return () => {
      window.clearTimeout(timer);
      c.abort();
    };
  }, [typing, chips]);

  const commit = (a: Address) => {
    if (!a.email && !a.name) return;
    onChange(joinAddresses([...chips, a]));
    setTyping('');
    setHits([]);
  };

  const commitTyping = () => {
    const raw = typing.trim().replace(/[,;]$/, '');
    if (!raw) return false;
    if (hits.length && active < hits.length && !isValidEmail(raw)) {
      commit({ name: hits[active].name, email: hits[active].email });
      return true;
    }
    commit(parseAddress(raw));
    return true;
  };

  const remove = (i: number) => onChange(joinAddresses(chips.filter((_, j) => j !== i)));

  return (
    <div className="fs-mail-compose__row">
      <span className="fs-mail-compose__label">{label}</span>
      <div className="fs-mail-compose__chips" data-testid={testId}>
        {chips.map((a, i) => (
          <span key={`${a.email}-${i}`} className="fs-mail-compose__chip" data-invalid={!isValidEmail(a.email) || undefined} title={a.email}>
            {a.name || a.email}
            <IconButton icon={X} label={t('Remove {who}', { who: a.name || a.email })} size="sm" onClick={() => remove(i)} />
          </span>
        ))}
        <input
          ref={inputRef}
          type="text"
          value={typing}
          autoFocus={autoFocus}
          aria-label={label}
          aria-autocomplete="list"
          aria-expanded={hits.length > 0}
          placeholder={chips.length ? '' : t('name@domain')}
          onChange={(e) => setTyping(e.target.value)}
          onBlur={() => commitTyping()}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ',' || e.key === ';' || e.key === 'Tab') {
              if (commitTyping()) e.preventDefault();
            } else if (e.key === 'Backspace' && !typing && chips.length) {
              remove(chips.length - 1);
            } else if (e.key === 'ArrowDown' && hits.length) {
              e.preventDefault();
              setActive((a) => (a + 1) % hits.length);
            } else if (e.key === 'ArrowUp' && hits.length) {
              e.preventDefault();
              setActive((a) => (a - 1 + hits.length) % hits.length);
            } else if (e.key === 'Escape' && hits.length) {
              setHits([]);
            }
          }}
        />
        {hits.length > 0 && (
          <ul className="fs-mail-compose__hits" role="listbox">
            {hits.map((h, i) => (
              <li key={`${h.email}-${i}`} role="option" aria-selected={i === active}>
                <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => commit({ name: h.name, email: h.email })} data-active={i === active || undefined}>
                  <Avatar name={h.name} email={h.email} size="sm" />
                  <span>
                    <b>{h.name || h.email}</b>
                    {h.name && <small>{h.email}</small>}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/* ── Attach from the library ── */

function LibraryPicker({ onPick, onClose }: { onPick: (items: { kind: 'document' | 'gallery'; id: string; label: string }[], zip: boolean) => void; onClose: () => void }) {
  const [tab, setTab] = useState<'document' | 'gallery'>('document');
  const [search, setSearch] = useState('');
  const [docs, setDocs] = useState<LibraryDoc[] | null>(null);
  const [images, setImages] = useState<GalleryImage[] | null>(null);
  const [picked, setPicked] = useState<Map<string, { kind: 'document' | 'gallery'; id: string; label: string }>>(new Map());
  const [zip, setZip] = useState(true);

  useEffect(() => {
    const c = new AbortController();
    const timer = window.setTimeout(() => {
      if (tab === 'document') loadDocLibrary({ search, limit: 40 }, c.signal).then((r) => setDocs(r.documents)).catch(() => setDocs([]));
      else loadGallery({ search, limit: 40 }, c.signal).then((r) => setImages(r.items)).catch(() => setImages([]));
    }, 250);
    return () => {
      window.clearTimeout(timer);
      c.abort();
    };
  }, [tab, search]);

  const toggle = (kind: 'document' | 'gallery', id: string, label: string) =>
    setPicked((m) => {
      const n = new Map(m);
      const key = `${kind}:${id}`;
      if (n.has(key)) n.delete(key);
      else n.set(key, { kind, id, label });
      return n;
    });

  const list = tab === 'document' ? docs : images;
  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={t('Attach from the library')}
      testId="mail-library-picker"
      footer={
        <>
          {picked.size > 1 && (
            <label className="fs-switch">
              <input type="checkbox" checked={zip} onChange={(e) => setZip(e.target.checked)} />
              <span>{t('As one zip')}</span>
            </label>
          )}
          <span className="fs-spacer" />
          <Button variant="ghost" size="sm" label={t('Cancel')} onClick={onClose} />
          <Button variant="primary" size="sm" label={picked.size ? t('Attach {n}', { n: picked.size }) : t('Attach')} disabled={!picked.size} onClick={() => onPick([...picked.values()], zip && picked.size > 1)} />
        </>
      }
    >
      <div className="fs-seg" role="radiogroup" aria-label={t('Kind')}>
        <button type="button" role="radio" aria-checked={tab === 'document'} onClick={() => setTab('document')}>
          <FileText size={12} aria-hidden="true" /> {t('Documents')}
        </button>
        <button type="button" role="radio" aria-checked={tab === 'gallery'} onClick={() => setTab('gallery')}>
          <ImageIcon size={12} aria-hidden="true" /> {t('Images')}
        </button>
      </div>
      <input type="search" className="fs-field" value={search} onChange={(e) => setSearch(e.target.value)} placeholder={t('Search')} aria-label={t('Search')} />
      {!list && <Skeleton label={t('Loading')} count={4} height="36px" />}
      {list && list.length === 0 && <p className="fs-muted">{t('Nothing here.')}</p>}
      {list && list.length > 0 && (
        <ul className="fs-mail-compose__lib">
          {(tab === 'document' ? (docs ?? []) : []).map((d) => (
            <li key={d.id}>
              <label className="fs-switch">
                <input type="checkbox" checked={picked.has(`document:${d.id}`)} onChange={() => toggle('document', d.id, d.title)} />
                <span>
                  {d.title} <small>{d.language}</small>
                </span>
              </label>
            </li>
          ))}
          {(tab === 'gallery' ? (images ?? []) : []).map((img) => (
            <li key={img.id}>
              <label className="fs-switch">
                <input type="checkbox" checked={picked.has(`gallery:${img.id}`)} onChange={() => toggle('gallery', img.id, img.filename)} />
                <img className="fs-mail-compose__thumb" src={img.url} alt="" loading="lazy" />
                <span>{img.filename || img.caption || img.prompt.slice(0, 40)}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
    </Dialog>
  );
}

/* ── Composer ── */

export function Compose({ seed, accounts, accountId, onClose, onDone, say }: ComposeProps) {
  const [to, setTo] = useState(seed.to);
  const [cc, setCc] = useState(seed.cc);
  const [bcc, setBcc] = useState(seed.bcc);
  const [subject, setSubject] = useState(seed.subject);
  const [body, setBody] = useState(seed.body);
  const [showCc, setShowCc] = useState(Boolean(seed.cc || seed.bcc));
  const [from, setFrom] = useState(accountId ?? accounts.find((a) => a.isDefault)?.id ?? accounts[0]?.id ?? '');
  const [attachments, setAttachments] = useState<StagedAttachment[]>(seed.attachments);
  const [staging, setStaging] = useState(0);
  const [busy, setBusy] = useState<'send' | 'draft' | 'schedule' | 'ai' | null>(null);
  const [hint, setHint] = useState('');
  const [aiModel, setAiModel] = useState('');
  const [picker, setPicker] = useState(false);
  const [when, setWhen] = useState<string | null>(null);
  const [confirmNoFile, setConfirmNoFile] = useState<null | (() => void)>(null);
  const bodyRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const uploadedHere = useRef<Set<string>>(new Set());
  const dirty = to !== seed.to || subject !== seed.subject || body !== seed.body || attachments.length !== seed.attachments.length;

  useEffect(() => {
    if (!seed.forwardFrom || !seed.forwardFrom.attachments.length) return;
    let cancelled = false;
    const run = async () => {
      setStaging(seed.forwardFrom!.attachments.length);
      for (const a of seed.forwardFrom!.attachments) {
        try {
          const staged = await attachFromMail(seed.forwardFrom!.uid, a.index, { folder: seed.forwardFrom!.folder, accountId });
          if (cancelled) return;
          uploadedHere.current.add(staged.token);
          setAttachments((cur) => [...cur, staged]);
        } catch (err) {
          say((err as Error).message || t('Could not attach {name}.', { name: a.filename }), 'warn');
        } finally {
          setStaging((n) => Math.max(0, n - 1));
        }
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed.forwardFrom]);

  const outgoing = (): Outgoing => ({
    to: joinAddresses(splitAddresses(to)),
    cc: joinAddresses(splitAddresses(cc)),
    bcc: joinAddresses(splitAddresses(bcc)),
    subject: subject.trim(),
    body,
    inReplyTo: seed.inReplyTo,
    references: seed.references,
    accountId: from || null,
    sourceUid: seed.sourceUid,
    sourceFolder: seed.sourceFolder,
    attachments: attachments.map((a) => a.token),
  });

  const validRecipients = (): boolean => {
    const all = [...splitAddresses(to), ...splitAddresses(cc), ...splitAddresses(bcc)];
    if (!splitAddresses(to).length) {
      say(t('The recipient is missing.'), 'warn');
      return false;
    }
    const bad = all.find((a) => !isValidEmail(a.email));
    if (bad) {
      say(t('{who} is not a valid address.', { who: formatAddress(bad) }), 'warn');
      return false;
    }
    return true;
  };

  const remember = () => {
    for (const a of [...splitAddresses(to), ...splitAddresses(cc)]) if (a.email && a.name) void rememberContact(a.name, a.email);
  };

  const guardAttachments = (go: () => void) => {
    if (!attachments.length && mentionsAttachment(body.split('\n> ')[0])) setConfirmNoFile(() => go);
    else go();
  };

  const send = () => {
    if (!validRecipients()) return;
    guardAttachments(async () => {
      setBusy('send');
      try {
        await sendEmail(outgoing());
        remember();
        onDone('sent', seed);
      } catch (err) {
        say((err as Error).message || t('Could not send.'), 'warn');
      } finally {
        setBusy(null);
      }
    });
  };

  const draft = async () => {
    setBusy('draft');
    try {
      await saveDraft(outgoing());
      onDone('draft', seed);
    } catch (err) {
      say((err as Error).message || t('Could not save the draft.'), 'warn');
    } finally {
      setBusy(null);
    }
  };

  const schedule = (date: Date) => {
    if (!validRecipients()) return;
    if (date.getTime() <= Date.now()) {
      say(t('Pick a time in the future.'), 'warn');
      return;
    }
    guardAttachments(async () => {
      setBusy('schedule');
      try {
        await scheduleEmail(outgoing(), date);
        remember();
        setWhen(null);
        say(t('Scheduled for {when}.', { when: date.toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' }) }));
        onDone('scheduled', seed);
      } catch (err) {
        say((err as Error).message || t('Could not schedule.'), 'warn');
      } finally {
        setBusy(null);
      }
    });
  };

  const inHours = (h: number) => {
    const d = new Date();
    d.setMinutes(0, 0, 0);
    d.setHours(d.getHours() + h);
    return d;
  };
  const tomorrowMorning = () => {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    d.setHours(9, 0, 0, 0);
    return d;
  };
  const mondayMorning = () => {
    const d = new Date();
    d.setDate(d.getDate() + ((8 - d.getDay()) % 7 || 7));
    d.setHours(9, 0, 0, 0);
    return d;
  };

  const addFiles = async (files: FileList | File[]) => {
    const list = Array.from(files);
    if (!list.length) return;
    setStaging((n) => n + list.length);
    for (const f of list) {
      try {
        const staged = await uploadAttachment(f);
        uploadedHere.current.add(staged.token);
        setAttachments((cur) => [...cur, staged]);
      } catch (err) {
        say((err as Error).message || t('Could not attach {name}.', { name: f.name }), 'warn');
      } finally {
        setStaging((n) => Math.max(0, n - 1));
      }
    }
  };

  const addFromLibrary = async (items: { kind: 'document' | 'gallery'; id: string; label: string }[], zip: boolean) => {
    setPicker(false);
    setStaging((n) => n + 1);
    try {
      if (zip) {
        const staged = await attachManyAsZip(items.map(({ kind, id }) => ({ kind, id })));
        uploadedHere.current.add(staged.token);
        setAttachments((cur) => [...cur, staged]);
      } else {
        for (const it of items) {
          const staged = await attachFromLibrary(it.kind, it.id);
          uploadedHere.current.add(staged.token);
          setAttachments((cur) => [...cur, staged]);
        }
      }
      say(items.length > 1 ? t('Attached {n} items.', { n: items.length }) : t('Attached {name}.', { name: items[0].label }));
    } catch (err) {
      say((err as Error).message || t('Could not attach.'), 'warn');
    } finally {
      setStaging((n) => Math.max(0, n - 1));
    }
  };

  const removeAttachment = (a: StagedAttachment) => {
    setAttachments((cur) => cur.filter((x) => x.token !== a.token));
    if (uploadedHere.current.has(a.token)) void discardAttachment(a.token);
  };

  const discard = () => {
    for (const a of attachments) if (uploadedHere.current.has(a.token)) void discardAttachment(a.token);
    onClose();
  };

  const draftWithAi = async (fast: boolean) => {
    setBusy('ai');
    try {
      const r = await aiReply({
        to: joinAddresses(splitAddresses(to)),
        subject: subject.trim(),
        originalBody: seed.source?.body || t('(new mail, no original)'),
        uid: seed.source?.uid,
        folder: seed.source?.folder,
        accountId: from || null,
        messageId: seed.source?.messageId,
        fast,
        hint: hint.trim(),
      });
      const quoteAt = body.search(/\n\n(?:On |El |Le |Am |---------- )/);
      const quoted = quoteAt >= 0 ? body.slice(quoteAt) : '';
      setBody(`${r.reply.trim()}${quoted}`);
      setAiModel(r.model);
      say(t('Draft inserted ({model}).', { model: r.model || 'AI' }));
      bodyRef.current?.focus();
    } catch (err) {
      say((err as Error).message || t('Could not draft the reply.'), 'warn');
    } finally {
      setBusy(null);
    }
  };

  const onKey = useCallback(
    (e: React.KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        send();
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [to, cc, bcc, subject, body, attachments, from],
  );

  const title = seed.kind === 'reply' ? t('Reply') : seed.kind === 'forward' ? t('Forward') : t('New mail');
  const fromAccount = accounts.find((a) => a.id === from);

  return (
    <section className="fs-mail-compose" data-testid="compose" aria-label={title} onKeyDown={onKey}>
      <header className="fs-mail-compose__head">
        <h2 className="fs-mail-compose__title">{title}</h2>
        <span className="fs-spacer" />
        <IconButton icon={X} label={t('Close the composer')} size="sm" onClick={dirty ? discard : onClose} />
      </header>

      <div className="fs-mail-compose__fields">
        {accounts.length > 1 && (
          <label className="fs-mail-compose__row">
            <span className="fs-mail-compose__label">{t('From')}</span>
            <select className="fs-field" value={from} onChange={(e) => setFrom(e.target.value)}>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} {a.fromAddress ? `<${a.fromAddress}>` : ''}
                </option>
              ))}
            </select>
          </label>
        )}
        {fromAccount && !fromAccount.canSend && <p className="fs-notice" data-tone="warning">{t('This account has no SMTP or OAuth set up, so it cannot send. Check Settings → Integrations.')}</p>}
        <Recipients label={t('To')} value={to} onChange={setTo} autoFocus={!seed.to} testId="compose-to" />
        {!showCc && (
          <div className="fs-mail-compose__row fs-mail-compose__row--aside">
            <span className="fs-mail-compose__label" />
            <button type="button" className="fs-mail__link" onClick={() => setShowCc(true)}>
              CC / CCO
            </button>
          </div>
        )}
        {showCc && (
          <>
            <Recipients label="CC" value={cc} onChange={setCc} />
            <Recipients label="CCO" value={bcc} onChange={setBcc} />
          </>
        )}
        <label className="fs-mail-compose__row">
          <span className="fs-mail-compose__label">{t('Subject')}</span>
          <input type="text" className="fs-field" value={subject} onChange={(e) => setSubject(e.target.value)} data-testid="compose-subject" />
        </label>
      </div>

      <div className="fs-mail-compose__ai">
        <Popover
          trigger={<Button variant="secondary" size="sm" icon={Sparkles} label={aiModel ? t('Draft again with AI') : t('Draft with AI')} loading={busy === 'ai'} testId="compose-ai" />}
          align="start"
          className="fs-mail-compose__ai-pop"
        >
          <label className="fs-mail__field">
            <span>{t('What should it say? (optional)')}</span>
            <textarea className="fs-field" rows={3} value={hint} onChange={(e) => setHint(e.target.value)} placeholder={t('Accept, but ask for Friday instead…')} />
          </label>
          <div className="fs-inline">
            <Button variant="primary" size="sm" label={t('Quick draft')} title={t('Short and to the point')} onClick={() => void draftWithAi(true)} />
            <Button variant="secondary" size="sm" label={t('Thorough draft')} title={t('Reads the whole thread and answers every point')} onClick={() => void draftWithAi(false)} />
          </div>
          <p className="fs-muted">{t('Uses your writing style from the mail settings.')}</p>
        </Popover>
        {aiModel && <span className="fs-mail__ai-model">{aiModel}</span>}
      </div>

      <textarea
        ref={bodyRef}
        className="fs-mail-compose__body"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        autoFocus={Boolean(seed.to)}
        placeholder={t('Write here. Markdown works.')}
        data-testid="compose-body"
        onDrop={(e) => {
          if (e.dataTransfer.files.length) {
            e.preventDefault();
            void addFiles(e.dataTransfer.files);
          }
        }}
        onDragOver={(e) => {
          if (e.dataTransfer.types.includes('Files')) e.preventDefault();
        }}
      />

      {(attachments.length > 0 || staging > 0) && (
        <ul className="fs-mail-compose__atts" aria-label={t('Attachments')}>
          {attachments.map((a) => (
            <li key={a.token} className="fs-mail-compose__att">
              <Paperclip size={12} aria-hidden="true" />
              <span className="fs-mail__att-name">{a.filename}</span>
              {a.size > 0 && <span className="fs-mail__att-size">{bytesLabel(a.size)}</span>}
              <IconButton icon={X} label={t('Remove {who}', { who: a.filename })} size="sm" onClick={() => removeAttachment(a)} />
            </li>
          ))}
          {staging > 0 && <li className="fs-mail-compose__att fs-mail-compose__att--busy">{t('Attaching…')}</li>}
        </ul>
      )}

      <footer className="fs-mail-compose__foot">
        <input ref={fileRef} type="file" multiple hidden onChange={(e) => e.target.files && void addFiles(e.target.files)} />
        <Menu
          trigger={<Button variant="ghost" size="sm" icon={Paperclip} label={t('Attach')} testId="compose-attach" />}
          items={[
            { label: t('A file from this computer…'), icon: Paperclip, onSelect: () => fileRef.current?.click() },
            { label: t('From the library…'), icon: Library, onSelect: () => setPicker(true) },
          ]}
        />
        <Button variant="ghost" size="sm" label={t('Save draft')} loading={busy === 'draft'} onClick={() => void draft()} />
        <span className="fs-spacer" />
        <Button variant="ghost" size="sm" icon={Trash2} label={t('Discard')} onClick={discard} />
        <Menu
          trigger={<Button variant="secondary" size="sm" icon={Clock} label={t('Send later')} loading={busy === 'schedule'} testId="compose-later" />}
          align="end"
          items={[
            { label: t('In an hour'), onSelect: () => schedule(inHours(1)) },
            { label: `${t('Tomorrow morning')} · 9:00`, onSelect: () => schedule(tomorrowMorning()) },
            { label: `${t('Monday morning')} · 9:00`, onSelect: () => schedule(mondayMorning()) },
            null,
            { label: t('Pick a time…'), onSelect: () => setWhen('') },
          ]}
        />
        <Button variant="primary" size="sm" icon={Send} label={t('Send')} loading={busy === 'send'} onClick={send} testId="compose-send" title="Ctrl+Enter" />
      </footer>

      {picker && <LibraryPicker onPick={(items, zip) => void addFromLibrary(items, zip)} onClose={() => setPicker(false)} />}

      {when !== null && (
        <Dialog open onOpenChange={(o) => !o && setWhen(null)} title={t('Send later')} testId="compose-when" footer={<Button variant="primary" size="sm" label={t('Schedule')} disabled={!when} loading={busy === 'schedule'} onClick={() => schedule(new Date(when))} />}>
          <label className="fs-mail__field">
            <span>{t('When')}</span>
            <input type="datetime-local" className="fs-field" value={when} onChange={(e) => setWhen(e.target.value)} autoFocus />
          </label>
        </Dialog>
      )}

      {confirmNoFile && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirmNoFile(null)}
          title={t('Send without an attachment?')}
          description={t('The text mentions an attachment but nothing is attached.')}
          testId="compose-no-file"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Attach something')} onClick={() => { setConfirmNoFile(null); fileRef.current?.click(); }} />
              <Button variant="primary" size="sm" label={t('Send anyway')} onClick={() => { const go = confirmNoFile; setConfirmNoFile(null); go(); }} />
            </>
          }
        />
      )}
    </section>
  );
}
