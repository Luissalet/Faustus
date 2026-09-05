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
} from '../adapters/email';
import './projects.css';
import './email.css';

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
  return name && name !== address ? name : address || '(desconocido)';
}

function fmtWhen(date: string): string {
  if (!date) return '';
  const d = new Date(date);
  if (Number.isNaN(d.getTime())) return date;
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return d.toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' });
  if (d.getFullYear() === today.getFullYear()) return d.toLocaleDateString('es', { day: 'numeric', month: 'short' });
  return d.toLocaleDateString('es', { day: 'numeric', month: 'short', year: 'numeric' });
}

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/* ── Compose ── */

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
      say('Falta el destinatario.');
      return;
    }
    setBusy('send');
    try {
      await sendEmail(outgoing());
      onDone('sent');
    } catch (err) {
      say((err as Error).message || 'No se ha podido enviar.');
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
      say((err as Error).message || 'No se ha podido guardar el borrador.');
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
      title={s.inReplyTo ? 'Responder' : s.subject.startsWith('Fwd:') ? 'Reenviar' : 'Nuevo correo'}
      testId="compose"
      footer={
        <div className="fs-mail-compose__foot">
          <Button variant="ghost" size="sm" label="Guardar borrador" loading={busy === 'draft'} onClick={() => void draft()} />
          <span className="fs-mail__spacer" />
          <Button variant="ghost" size="sm" label="Descartar" onClick={onClose} />
          <Button variant="primary" size="sm" label="Enviar" loading={busy === 'send'} onClick={() => void send()} testId="compose-send" />
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
          <input type="text" className="fs-field" value={s.to} onChange={(e) => set({ to: e.target.value })} placeholder="nombre@dominio, otro@dominio" autoFocus={!s.to} />
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
        <textarea className="fs-mail-compose__body" rows={12} value={s.body} onChange={(e) => set({ body: e.target.value })} autoFocus={Boolean(s.to)} placeholder="Escribe aquí. Markdown vale." />
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
    const base = '<base target="_blank"><style>body{margin:0;padding:12px 14px;font:14px/1.5 system-ui,sans-serif;color:#1b1b1f;background:#fff;overflow-wrap:anywhere}img{max-width:100%;height:auto}pre{white-space:pre-wrap}blockquote{border-left:3px solid #ccc;margin:8px 0;padding-left:10px;color:#555}</style>';
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
      say((err as Error).message || 'La operación ha fallado.');
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
    body: `\n\n---------- Mensaje reenviado ----------\nDe: ${fmtAddr(mail.fromName, mail.fromAddress)} <${mail.fromAddress}>\nFecha: ${mail.date ? new Date(mail.date).toLocaleString('es') : ''}\nAsunto: ${mail.subject}\nPara: ${mail.to}\n\n${mail.body}`,
  });

  const moveTargets = folders.filter((f) => f !== mail.folder);

  return (
    <article className="fs-mail__msg" data-testid="mail-message">
      <header className="fs-mail__msg-bar">
        <IconButton icon={ArrowLeft} label="Volver a la lista" size="sm" onClick={onBack} />
        <div className="fs-mail__msg-actions">
          <Button variant="secondary" size="sm" icon={Reply} label="Responder" onClick={() => onCompose(replyState(false))} />
          <IconButton icon={ReplyAll} label="Responder a todos" size="sm" onClick={() => onCompose(replyState(true))} />
          <IconButton icon={Forward} label="Reenviar" size="sm" onClick={() => onCompose(forwardState())} />
          <span className="fs-mail__sep" />
          <IconButton icon={Archive} label="Archivar" size="sm" disabled={busy !== null} onClick={() => void act('archive', () => archiveEmail(mail.uid, mail.folder, accountId), 'gone', 'Archivado.')} />
          <IconButton icon={Trash2} label="Borrar" size="sm" disabled={busy !== null} onClick={() => void act('delete', () => deleteEmail(mail.uid, mail.folder, accountId), 'gone', 'Borrado.')} />
          <IconButton icon={mail.isFlagged ? StarOff : Star} label={mail.isFlagged ? 'Quitar la estrella' : 'Marcar con estrella'} size="sm" disabled={busy !== null} onClick={() => void act('flag', () => flagEmail(mail.uid, !mail.isFlagged, mail.folder, accountId), { isFlagged: !mail.isFlagged }, mail.isFlagged ? 'Sin estrella.' : 'Con estrella.')} />
          <IconButton icon={Mail} label="Marcar como no leído" size="sm" disabled={busy !== null} onClick={() => void act('unread', () => markUnread(mail.uid, mail.folder, accountId), { isRead: false }, 'Marcado como no leído.')} />
          {moveTargets.length > 0 && (
            <QuickMenu
              label="Mover a…"
              icon={Inbox}
              items={moveTargets.map((f) => ({ label: folderLabel(f), onSelect: () => void act('move', () => moveEmail(mail.uid, f, mail.folder, accountId), 'gone', `Movido a ${folderLabel(f)}.`) }))}
            />
          )}
        </div>
      </header>
      <h2 className="fs-mail__subject">{mail.subject}</h2>
      <div className="fs-mail__meta">
        <span className="fs-mail__from">
          <b>{fmtAddr(mail.fromName, mail.fromAddress)}</b> {mail.fromAddress && mail.fromName && <span className="fs-mail__addr">&lt;{mail.fromAddress}&gt;</span>}
        </span>
        <span className="fs-mail__when">{mail.date ? new Date(mail.date).toLocaleString('es') : ''}</span>
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
      <iframe ref={frameRef} className="fs-mail__frame" title="Cuerpo del correo" sandbox="allow-popups allow-popups-to-escape-sandbox" srcDoc={html} style={{ blockSize: frameH }} onLoad={onFrameLoad} />
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
  const [compose, setCompose] = useState<ComposeState | null>(null);
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
        if ((err as { name?: string })?.name !== 'AbortError') setListError((err as Error).message || 'No he podido leer el correo.');
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
      say((err as Error).message || 'No he podido abrir el correo.');
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
        title="No he podido leer las cuentas de correo"
        body="El endpoint de correo no responde. La interfaz anterior no depende de esta pantalla."
        primaryAction={{
          label: 'Abrir la interfaz anterior',
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
        title="Sin cuentas de correo"
        body="Las cuentas (IMAP/SMTP o Google) se configuran por ahora en los ajustes de la interfaz anterior."
        primaryAction={{
          label: 'Configurar en la interfaz anterior',
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
          <h1 className="fs-screen__title">Correo</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {emails ? `${total} en ${folderLabel(folder)}${unread ? ` · ${unread} sin leer en esta página` : ''}.` : 'Leer, contestar y ordenar.'}
          </p>
        </div>
        <div className="fs-mail__tools">
          <IconButton
            icon={RefreshCw}
            label="Actualizar"
            size="sm"
            disabled={refreshing}
            onClick={() => {
              setRefreshing(true);
              setReload((n) => n + 1);
            }}
          />
          <IconButton
            icon={Settings2}
            label="Cuentas y reglas (interfaz anterior)"
            size="sm"
            onClick={() => {
              window.location.href = '/email?shell=legacy';
            }}
          />
          <Button variant="primary" size="sm" icon={PenLine} label="Redactar" onClick={() => setCompose({ to: '', cc: '', bcc: '', subject: '', body: '' })} testId="mail-compose" />
        </div>
      </header>

      <div className="fs-mail__layout">
        <aside className="fs-mail__side" aria-label="Cuentas y carpetas">
          {accounts && accounts.length > 1 && (
            <select className="fs-field fs-mail__account" value={accountId ?? ''} onChange={(e) => { setAccountId(e.target.value || null); setOffset(0); setCurrent(null); }} aria-label="Cuenta">
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
          <div className="fs-mail__filters" role="group" aria-label="Filtro">
            {(['all', 'unread', 'unanswered', 'favorites'] as ListFilter[]).map((f) => (
              <button key={f} type="button" className="fs-chip" data-on={filter === f || undefined} onClick={() => { setFilter(f); setOffset(0); }}>
                {f === 'all' ? 'Todos' : f === 'unread' ? 'Sin leer' : f === 'unanswered' ? 'Sin contestar' : 'Con estrella'}
              </button>
            ))}
          </div>
        </aside>

        <section className="fs-mail__list" aria-label="Mensajes">
          <label className="fs-mail__search">
            <Search size={13} aria-hidden="true" />
            <input type="search" placeholder={`Buscar en ${folderLabel(folder)}…`} value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Buscar" />
            {query && <IconButton icon={X} label="Limpiar" size="sm" onClick={() => setQuery('')} />}
          </label>
          {listError && <p className="fs-mail__error">{listError}</p>}
          {!list && !listError && <Skeleton label="Cargando mensajes" count={6} height="56px" />}
          {searching && <p className="fs-mail__hint">Buscando…</p>}
          {list && list.length === 0 && !listError && <p className="fs-mail__hint">{results ? 'Nada coincide.' : 'Vacío.'}</p>}
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
                    {m.isFlagged && <Star size={11} className="fs-mail__star" aria-label="Con estrella" />}
                    {m.hasAttachments && <Paperclip size={11} aria-label="Con adjuntos" />}
                    {m.subject}
                  </span>
                  {m.snippet && <span className="fs-mail__row-snippet">{m.snippet}</span>}
                </button>
              ))}
            </div>
          )}
          {!results && emails && total > PAGE && (
            <div className="fs-mail__pager">
              <Button variant="ghost" size="sm" label="Anteriores" disabled={offset === 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE))} />
              <span>
                {offset + 1}–{Math.min(offset + PAGE, total)} de {total}
              </span>
              <Button variant="ghost" size="sm" label="Siguientes" disabled={offset + PAGE >= total} onClick={() => setOffset((o) => o + PAGE)} />
            </div>
          )}
        </section>

        <section className="fs-mail__read" aria-label="Mensaje">
          {!current && (
            <div className="fs-mail__placeholder">
              <MailOpen size={28} aria-hidden="true" />
              <p>Elige un mensaje.</p>
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
            say(what === 'sent' ? 'Enviado.' : 'Borrador guardado.');
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
