import { Check, CheckCheck, Clock, Copy, Forward, Pencil, Reply, Smile, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Button, IconButton, Popover } from '../../components';
import { waAvatarUrl, waDelete, waEdit, waForward, waMediaUrl, waReact, waTranscribe, type WaChat, type WaMessage } from '../../adapters/whatsapp';
import { t } from '../../i18n';

/* ── Avatar: a chat's profile picture, or an initial-letter fallback ── */

export function Avatar({ jid, name, size = 'list' }: { jid: string; name: string; size?: 'list' | 'head' }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    const initial = (name || jid || '?').trim().charAt(0).toUpperCase() || '?';
    return (
      <span className="fs-wa__avatar fs-wa__avatar--fallback" data-size={size} aria-hidden="true">
        {initial}
      </span>
    );
  }
  return (
    <img
      className="fs-wa__avatar"
      data-size={size}
      src={waAvatarUrl(jid)}
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

const KIND_LABEL: Record<string, string> = {
  image: '[photo]',
  video: '[video]',
  audio: '[audio]',
  document: '[document]',
  sticker: '[sticker]',
  location: '[location]',
  contact: '[contact]',
  reaction: '[reaction]',
  other: '[attachment]',
};

function messageBody(m: WaMessage): string {
  if (m.kind === 'text') return m.text || '';
  return KIND_LABEL[m.kind] ?? KIND_LABEL.other;
}

export function formatTime(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}

function formatDuration(seconds: number | undefined): string {
  const total = Math.max(0, Math.round(seconds ?? 0));
  const mm = Math.floor(total / 60);
  const ss = String(total % 60).padStart(2, '0');
  return `${mm}:${ss}`;
}

/** "Hoy" / "Ayer" / the date — the separator between two days of history. */
export function dayLabel(ts: number, locale: string): string {
  const d = new Date(ts * 1000);
  const now = new Date();
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (diffDays === 0) return t('Today');
  if (diffDays === 1) return t('Yesterday');
  try {
    return d.toLocaleDateString(locale, { day: 'numeric', month: 'long', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' });
  } catch {
    return d.toLocaleDateString();
  }
}

/* ── Delivery ticks on own messages ── */

function StatusTicks({ status }: { status?: WaMessage['status'] }) {
  if (!status) return null;
  if (status === 'pending') {
    return <span className="fs-wa__tick" title={t('Sending…')}><Clock size={12} aria-hidden="true" /></span>;
  }
  if (status === 'sent') {
    return <span className="fs-wa__tick" title={t('Sent')}><Check size={13} aria-hidden="true" /></span>;
  }
  if (status === 'delivered') {
    return <span className="fs-wa__tick" title={t('Delivered')}><CheckCheck size={13} aria-hidden="true" /></span>;
  }
  if (status === 'read' || status === 'played') {
    return (
      <span className="fs-wa__tick fs-wa__tick--read" title={status === 'played' ? t('Played') : t('Read')}>
        <CheckCheck size={13} aria-hidden="true" />
      </span>
    );
  }
  return null;
}

/* ── Reactions row, grouped by emoji ── */

function Reactions({ reactions }: { reactions?: WaMessage['reactions'] }) {
  if (!reactions || reactions.length === 0) return null;
  const groups = new Map<string, number>();
  for (const r of reactions) groups.set(r.emoji, (groups.get(r.emoji) ?? 0) + 1);
  return (
    <span className="fs-wa__reactions" data-testid="whatsapp-reactions">
      {[...groups.entries()].map(([emoji, count]) => (
        <span className="fs-wa__reaction" key={emoji}>
          {emoji} {count > 1 ? count : ''}
        </span>
      ))}
    </span>
  );
}

/* ── Quoted "reply to" block above a bubble ── */

function QuotedBlock({ reply, onJump }: { reply?: WaMessage['reply_to']; onJump: (id: string) => void }) {
  if (!reply) return null;
  return (
    <button type="button" className="fs-wa__quote" onClick={() => onJump(reply.id)} data-testid="whatsapp-quote">
      <strong>{reply.from_me || reply.from_name === 'me' ? t('You') : reply.from_name}</strong>
      <span>{reply.text}</span>
    </button>
  );
}

/* ── Voice notes: player + on-demand transcription ── */

function AudioBubble({ msg, transcript, onTranscribed, say }: { msg: WaMessage; transcript?: string; onTranscribed: (id: string, text: string) => void; say: (msg: string) => void }) {
  const [busy, setBusy] = useState(false);
  const text = transcript ?? msg.transcript;

  const doTranscribe = async () => {
    setBusy(true);
    try {
      const r = await waTranscribe(msg.id);
      onTranscribed(msg.id, r.text);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {msg.media && <audio controls preload="none" src={waMediaUrl(msg.media)} className="fs-wa__audio" data-testid="whatsapp-audio" />}
      <span className="fs-set__help">{formatDuration(msg.seconds)}</span>
      {text ? (
        <p className="fs-wa__transcript">{text}</p>
      ) : (
        <Button size="sm" variant="ghost" label={t('Transcribe')} loading={busy} onClick={() => void doTranscribe()} testId="whatsapp-transcribe" />
      )}
    </>
  );
}

function BubbleContent({ msg, transcript, onTranscribed, say, onOpenImage }: { msg: WaMessage; transcript?: string; onTranscribed: (id: string, text: string) => void; say: (msg: string) => void; onOpenImage?: (src: string, caption: string) => void }) {
  if (msg.deleted) return <span className="fs-wa__deleted">{t('This message was deleted')}</span>;
  if (msg.kind === 'text') return <span>{msg.text}</span>;
  if (msg.kind === 'audio' && msg.media) return <AudioBubble msg={msg} transcript={transcript} onTranscribed={onTranscribed} say={say} />;
  if (msg.kind === 'image' && msg.media) {
    return (
      <>
        <button
          type="button"
          className="fs-wa__image-link"
          onClick={() => onOpenImage?.(waMediaUrl(msg.media as string), msg.text || '')}
          aria-label={t('Open photo')}
          data-testid="whatsapp-image-open"
        >
          <img src={waMediaUrl(msg.media)} className="fs-wa__image" loading="lazy" alt="" />
        </button>
        {msg.text && <span>{msg.text}</span>}
      </>
    );
  }
  if (msg.kind === 'document' && msg.media) {
    return (
      <a href={waMediaUrl(msg.media)} download className="fs-wa__doc-link">
        {msg.text || msg.media}
      </a>
    );
  }
  return <span className="fs-set__help">{messageBody(msg)}</span>;
}

const QUICK_EMOJI = ['👍', '❤️', '😂', '😮', '😢', '🙏'];

function ForwardPicker({ chats, onPick }: { chats: WaChat[]; onPick: (to: string) => void }) {
  const [q, setQ] = useState('');
  const query = q.trim().toLowerCase();
  const list = query ? chats.filter((c) => (c.name || c.jid).toLowerCase().includes(query)) : chats;
  return (
    <div className="fs-wa__forward-picker" data-testid="whatsapp-forward-picker">
      <input
        type="search"
        value={q}
        placeholder={t('Filter chats…')}
        aria-label={t('Filter')}
        onChange={(e) => setQ(e.target.value)}
        autoFocus
      />
      <ul className="fs-wa__forward-list">
        {list.slice(0, 30).map((c) => (
          <li key={c.jid}>
            <button type="button" onClick={() => onPick(c.jid)}>
              {c.name || c.jid}
            </button>
          </li>
        ))}
        {list.length === 0 && <li className="fs-set__help">{t('No chats match this filter yet.')}</li>}
      </ul>
    </div>
  );
}

interface BubbleActionsProps {
  msg: WaMessage;
  chats: WaChat[];
  onReply: () => void;
  onChanged: () => void;
  onEditStart: () => void;
  say: (msg: string) => void;
}

function BubbleActions({ msg, chats, onReply, onChanged, onEditStart, say }: BubbleActionsProps) {
  const [confirmDelete, setConfirmDelete] = useState(false);

  const react = async (emoji: string) => {
    try {
      await waReact(msg.id, emoji);
      onChanged();
    } catch (e) {
      say((e as Error).message);
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(msg.text || '');
    } catch (e) {
      say((e as Error).message);
    }
  };

  const forward = async (to: string) => {
    try {
      await waForward(msg.id, to);
      onChanged();
    } catch (e) {
      say((e as Error).message);
    }
  };

  const doDelete = async () => {
    try {
      await waDelete(msg.id);
      setConfirmDelete(false);
      onChanged();
    } catch (e) {
      say((e as Error).message);
    }
  };

  if (confirmDelete) {
    return (
      <span className="fs-modes__confirm fs-wa__bubble-actions" data-testid="whatsapp-delete-confirm">
        {t('Delete for everyone?')}
        <Button size="sm" variant="danger" label={t('Confirm')} onClick={() => void doDelete()} />
        <Button size="sm" variant="ghost" label={t('Cancel')} onClick={() => setConfirmDelete(false)} />
      </span>
    );
  }

  return (
    <span className="fs-wa__bubble-actions">
      <IconButton icon={Reply} label={t('Reply')} size="sm" onClick={onReply} testId="whatsapp-reply" />
      <Popover
        trigger={<IconButton icon={Smile} label={t('React')} size="sm" testId="whatsapp-react" />}
        testId="whatsapp-react-popover"
      >
        <span className="fs-wa__emoji-row">
          {QUICK_EMOJI.map((e) => (
            <button type="button" key={e} className="fs-wa__emoji-btn" onClick={() => void react(e)}>
              {e}
            </button>
          ))}
        </span>
      </Popover>
      <Popover
        trigger={<IconButton icon={Forward} label={t('Forward')} size="sm" testId="whatsapp-forward" />}
        testId="whatsapp-forward-popover"
      >
        <ForwardPicker chats={chats} onPick={(to) => void forward(to)} />
      </Popover>
      <IconButton icon={Copy} label={t('Copy')} size="sm" onClick={() => void copy()} />
      {msg.from_me && msg.kind === 'text' && !msg.deleted && (
        <>
          <IconButton icon={Pencil} label={t('Edit')} size="sm" onClick={onEditStart} />
          <IconButton icon={Trash2} label={t('Delete for everyone')} size="sm" onClick={() => setConfirmDelete(true)} />
        </>
      )}
    </span>
  );
}

function EditingBubble({ msg, onDone, say }: { msg: WaMessage; onDone: () => void; say: (msg: string) => void }) {
  const [text, setText] = useState(msg.text);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    const value = text.trim();
    if (!value) return;
    setBusy(true);
    try {
      await waEdit(msg.id, value);
      onDone();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="fs-wa__edit-row">
      <textarea
        className="fs-wa__edit-input"
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={2}
        autoFocus
        data-testid="whatsapp-edit-input"
      />
      <span className="fs-wa__edit-actions">
        <Button size="sm" variant="primary" label={t('Save')} loading={busy} disabled={!text.trim()} onClick={() => void save()} />
        <Button size="sm" variant="ghost" label={t('Cancel')} disabled={busy} onClick={onDone} />
      </span>
    </span>
  );
}

export interface MessageBubbleProps {
  msg: WaMessage;
  showSender: boolean;
  transcript?: string;
  onTranscribed: (id: string, text: string) => void;
  say: (msg: string) => void;
  chats: WaChat[];
  onReply: (msg: WaMessage) => void;
  onChanged: () => void;
  onJump: (id: string) => void;
  onOpenImage?: (src: string, caption: string) => void;
}

export function MessageBubble({ msg, showSender, transcript, onTranscribed, say, chats, onReply, onChanged, onJump, onOpenImage }: MessageBubbleProps) {
  const [editing, setEditing] = useState(false);
  return (
    <li className={`fs-wa__bubble-row${msg.from_me ? ' fs-wa__bubble-row--me' : ''}`} data-testid="whatsapp-message" id={`wa-msg-${msg.id}`}>
      <div className="fs-wa__bubble" data-mine={msg.from_me} tabIndex={0}>
        {showSender && !msg.from_me && <span className="fs-wa__bubble-sender">{msg.from_name}</span>}
        <QuotedBlock reply={msg.reply_to} onJump={onJump} />
        {msg.forwarded && <span className="fs-wa__forwarded-label">{t('Forwarded')}</span>}
        {editing ? (
          <EditingBubble msg={msg} onDone={() => { setEditing(false); onChanged(); }} say={say} />
        ) : (
          <BubbleContent msg={msg} transcript={transcript} onTranscribed={onTranscribed} say={say} onOpenImage={onOpenImage} />
        )}
        <Reactions reactions={msg.reactions} />
        <span className="fs-wa__bubble-foot">
          {msg.edited && !msg.deleted && <span className="fs-wa__edited-label">{t('edited')}</span>}
          <span className="fs-wa__bubble-time">{formatTime(msg.ts)}</span>
          {msg.from_me && <StatusTicks status={msg.status} />}
        </span>
        {!msg.deleted && !editing && (
          <BubbleActions msg={msg} chats={chats} onReply={() => onReply(msg)} onChanged={onChanged} onEditStart={() => setEditing(true)} say={say} />
        )}
      </div>
    </li>
  );
}
