import { Archive, ArchiveRestore, BellOff, BellRing, Check, MessagesSquare, Pin, PinOff, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, Dialog, IconButton, Popover } from '../../components';
import { followThread, setThread, startThread, suggestThreadTitle, type AgoraSubthread, type PinnedMessage, type ThreadState } from '../../adapters/agoraThreads';
import { t, tn } from '../../i18n';
import { ago } from '../agora/util';
import './agora-threads.css';

export const THREAD_STATE_LABEL: Record<ThreadState, string> = { open: 'Open', closed: 'Closed', archived: 'Archived' };

type Notify = (text: string, tone?: 'ok' | 'warn') => void;

/** Under the message a thread started from: how many answers it has and when the last one came. */
export function ThreadBadge({ thread, onOpen }: { thread: AgoraSubthread; onOpen: (threadId: number) => void }) {
  return (
    <button type="button" className="fs-agt-badge" data-state={thread.state} data-unread={thread.unread > 0 || undefined} data-testid="agora-thread-badge"
      onClick={() => onOpen(thread.thread_id)} title={thread.title}>
      <MessagesSquare size={14} aria-hidden="true" />
      <strong>{thread.title}</strong>
      <span>{tn(thread.replies, '{n} reply', '{n} replies')}</span>
      {thread.last_at && <span className="fs-muted">{t('last {when}', { when: ago(thread.last_at) })}</span>}
      {thread.state !== 'open' && <span className="fs-agt-state">{t(THREAD_STATE_LABEL[thread.state])}</span>}
      {thread.unread > 0 && <span className="fs-agt-new">{thread.unread}</span>}
    </button>
  );
}

/** A message that belongs to a stretch a thread started from. */
export function ThreadSpanNote({ threadId, onOpen }: { threadId: number; onOpen: (threadId: number) => void }) {
  return <button type="button" className="fs-agt-span" onClick={() => onOpen(threadId)}>{t('Part of a thread')}</button>;
}

/** Ask for the title of a new thread: suggested by the family's model, editable before creating it. */
export function ThreadStartDialog({ space, messageId, toMessageId, onClose, onStarted, notify }: {
  space: string; messageId: number | null; toMessageId?: number; onClose: () => void; onStarted: (thread: AgoraSubthread) => void; notify: Notify;
}) {
  const [title, setTitle] = useState('');
  const [first, setFirst] = useState('');
  const [source, setSource] = useState('');
  const [busy, setBusy] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  useEffect(() => {
    if (messageId === null) return;
    let alive = true;
    setTitle(''); setFirst(''); setSource(''); setSuggesting(true);
    suggestThreadTitle(space, messageId, toMessageId)
      .then((out) => { if (alive) { setTitle((old) => old || out.title); setSource(out.source); } })
      .catch(() => undefined).finally(() => { if (alive) setSuggesting(false); });
    return () => { alive = false; };
  }, [space, messageId, toMessageId]);
  const create = () => {
    if (messageId === null || busy) return;
    setBusy(true);
    startThread(space, { messageId, toMessageId, title: title.trim() || undefined, body: first.trim() || undefined })
      .then((out) => { onStarted(out.thread); onClose(); if (out.existing) notify(t('That message already had a thread.')); })
      .catch((e: Error) => notify(e.message, 'warn')).finally(() => setBusy(false));
  };
  return (
    <Dialog open={messageId !== null} onOpenChange={(next) => { if (!next) onClose(); }} testId="agora-thread-start" className="fs-agx-dialog"
      title={toMessageId && toMessageId !== messageId ? t('Start a thread from these messages') : t('Start a thread from this message')}
      description={t('The thread keeps the link to where it began, and the channel shows how many answers it has.')}
      footer={<><Button label={t('Cancel')} onClick={onClose} /><Button variant="primary" label={t('Start the thread')} loading={busy} onClick={create} testId="agora-thread-start-go" /></>}>
      <div className="fs-agt-form">
        <label>{t('Title')}
          <input value={title} maxLength={80} autoFocus placeholder={suggesting ? t('Suggesting a title…') : t('A short title')} data-testid="agora-thread-title"
            onChange={(e) => setTitle(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); create(); } }} />
        </label>
        {source && <p className="fs-muted">{source === 'model' ? t('Suggested from what was said. Change it if you like.') : t('Taken from the first line. Change it if you like.')}</p>}
        <label>{t('First answer (optional)')}
          <textarea rows={3} value={first} maxLength={20000} onChange={(e) => setFirst(e.target.value)} />
        </label>
      </div>
    </Dialog>
  );
}

/** The top of a thread's page: its title (editable), where it came from, following, closing and archiving. */
export function ThreadHeader({ space, thread, onChanged, onOrigin, notify }: {
  space: string; thread: AgoraSubthread; onChanged: (thread: AgoraSubthread) => void; onOrigin: () => void; notify: Notify;
}) {
  const [name, setName] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = (work: Promise<{ thread: AgoraSubthread }>) => {
    setBusy(true);
    work.then((out) => onChanged(out.thread)).catch((e: Error) => notify(e.message, 'warn')).finally(() => setBusy(false));
  };
  const rename = () => {
    const next = (name ?? '').trim();
    setName(null);
    if (next && next !== thread.title) run(setThread(space, thread.thread_id, { title: next }));
  };
  const set = (state: ThreadState) => run(setThread(space, thread.thread_id, { state }));
  return (
    <header className="fs-agt-head" data-state={thread.state} data-testid="agora-thread-header" aria-busy={busy || undefined}>
      <div className="fs-agt-head__main">
        {name === null
          ? <button type="button" className="fs-agt-head__title" title={t('Rename the thread')} onClick={() => setName(thread.title)}>{thread.title}</button>
          : <input className="fs-agt-head__input" value={name} maxLength={80} autoFocus onChange={(e) => setName(e.target.value)} onBlur={rename}
              onKeyDown={(e) => { if (e.key === 'Enter') rename(); if (e.key === 'Escape') setName(null); }} aria-label={t('Rename the thread')} />}
        <span className="fs-muted">
          <button type="button" className="fs-agx-link" onClick={onOrigin}>{t('From {author}: {text}', { author: thread.origin.author ?? '?', text: thread.origin.excerpt.slice(0, 70) })}</button>
          {thread.channel && <> · #{thread.channel}</>}
        </span>
      </div>
      {thread.state !== 'open' && <span className="fs-agt-state">{t(THREAD_STATE_LABEL[thread.state])}</span>}
      <IconButton size="sm" icon={thread.following ? BellRing : BellOff} label={thread.following ? t('Following: stop following') : t('Follow this thread')}
        onClick={() => run(followThread(space, thread.thread_id, !thread.following))} testId="agora-thread-follow" />
      {thread.state === 'open'
        ? <IconButton size="sm" icon={Check} label={t('Close the thread')} onClick={() => set('closed')} testId="agora-thread-close" />
        : <IconButton size="sm" icon={X} label={t('Open the thread again')} onClick={() => set('open')} testId="agora-thread-reopen" />}
      {thread.state === 'archived'
        ? <IconButton size="sm" icon={ArchiveRestore} label={t('Take out of the archive')} onClick={() => set('open')} />
        : <IconButton size="sm" icon={Archive} label={t('Archive the thread')} onClick={() => set('archived')} testId="agora-thread-archive" />}
    </header>
  );
}

/** The pinned messages of the conversation: a button in its header with the list. */
export function PinsButton({ pins, onJump, onUnpin }: { pins: PinnedMessage[]; onJump: (messageId: number) => void; onUnpin: (messageId: number) => void }) {
  if (!pins.length) return null;
  return (
    <Popover align="end" testId="agora-pins" className="fs-agt-pins"
      trigger={<button type="button" className="fs-agt-pinbtn" data-testid="agora-pins-button" aria-label={tn(pins.length, '{n} pinned message', '{n} pinned messages')}>
        <Pin size={13} aria-hidden="true" /> {pins.length}</button>}>
      <ul className="fs-agt-pinlist">
        {pins.map((pin) => (
          <li key={pin.message_id}>
            <button type="button" className="fs-agt-pinrow" onClick={() => onJump(pin.message_id)}>
              <strong>{pin.author}</strong><span>{pin.excerpt}</span>
            </button>
            <IconButton size="sm" icon={PinOff} label={t('Unpin')} onClick={() => onUnpin(pin.message_id)} />
          </li>
        ))}
      </ul>
    </Popover>
  );
}
