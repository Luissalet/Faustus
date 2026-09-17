import { ArrowLeft, MessageCircle, Send, Users } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, EmptyState, Skeleton, Toast } from '../../components';
import {
  waChats,
  waLogout,
  waMarkRead,
  waMessages,
  waSend,
  waStart,
  waStatus,
  waStop,
  type WaChat,
  type WaMessage,
  type WhatsAppStatus,
} from '../../adapters/whatsapp';
import { relativeTime } from '../../adapters/home';
import { t } from '../../i18n';
import '../projects.css';
import '../settings.css';
import '../connectors/connectors.css';
import './whatsapp.css';

/**
 * WhatsApp — Studio side of `routes/whatsapp_routes.py` +
 * `src/whatsapp_bridge.py`. A local Node bridge links the person's own
 * WhatsApp account (a linked device, exactly like WhatsApp Web): read what
 * people wrote, answer from a chat, get a daily digest card.
 *
 * Stop and Unlink are person actions — the same inline two-step confirm
 * pattern (never a native dialog) that Processes.tsx and Connectors.tsx
 * already follow.
 */

const STATUS_LABEL: Record<WhatsAppStatus['status'], string> = {
  stopped: 'Stopped',
  starting: 'Starting…',
  qr: 'Scan the QR',
  connected: 'Connected',
  disconnected: 'Disconnected',
  logged_out: 'Unlinked',
};

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

function formatTime(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}

function StatusPill({ status }: { status: WhatsAppStatus }) {
  const label =
    status.status === 'connected' && status.me?.name
      ? t('Connected as {name}', { name: status.me.name })
      : t(STATUS_LABEL[status.status] ?? status.status);
  const counts = status.counts;
  return (
    <span className="fs-wa__pill" data-status={status.status} data-testid="whatsapp-status-pill">
      <span className="fs-wa__pill-dot" aria-hidden="true" />
      {label}
      {counts && status.status === 'connected' && (
        <span className="fs-set__help">
          {t('{chats} chats · {messages} messages', { chats: counts.chats, messages: counts.messages })}
        </span>
      )}
    </span>
  );
}

/* ── Setup panel: everything shown when we are NOT looking at chats ── */

function SetupPanel({ status, onChanged, say }: { status: WhatsAppStatus; onChanged: () => void; say: (msg: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const [confirmUnlink, setConfirmUnlink] = useState(false);

  const doStart = async () => {
    setBusy(true);
    try {
      const r = await waStart();
      if (r.error) say(r.error);
      onChanged();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const doStop = async () => {
    setBusy(true);
    try {
      await waStop();
      setConfirmStop(false);
      onChanged();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const doUnlink = async () => {
    setBusy(true);
    try {
      await waLogout();
      setConfirmUnlink(false);
      onChanged();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (status.status === 'stopped') {
    return (
      <div className="fs-set__card fs-wa__setup" data-testid="whatsapp-setup-stopped">
        <p className="fs-prose">{t('Your own account through a local bridge, like WhatsApp Web: nothing runs until you start it, and it stays on this machine.')}</p>
        {status.node === false && (
          <p className="fs-wa__warn">{t('Node.js is not installed — the bridge cannot start.')}</p>
        )}
        {status.installed === false && status.node !== false && (
          <p className="fs-set__help">{t('The first start installs the bridge (Node.js needed) and can take a few minutes.')}</p>
        )}
        <Button
          variant="primary"
          label={t('Start the bridge')}
          loading={busy}
          disabled={status.node === false}
          onClick={() => void doStart()}
          testId="whatsapp-start"
        />
      </div>
    );
  }

  if (status.status === 'starting') {
    return (
      <div className="fs-set__card fs-wa__setup" data-testid="whatsapp-setup-starting">
        <span className="fs-wa__spinner" aria-hidden="true" />
        <p className="fs-prose">{t('Installing the bridge (first time)… this can take a few minutes.')}</p>
      </div>
    );
  }

  if (status.status === 'qr') {
    return (
      <div className="fs-set__card fs-wa__setup" data-testid="whatsapp-setup-qr">
        {status.qr && <img className="fs-wa__qr" src={status.qr} alt={t('WhatsApp QR code')} width={280} height={280} />}
        <ol className="fs-wa__steps">
          <li>{t('Open WhatsApp on your phone')}</li>
          <li>{t('Settings → Linked devices → Link a device')}</li>
          <li>{t('Scan this code')}</li>
        </ol>
      </div>
    );
  }

  if (status.status === 'logged_out') {
    return (
      <div className="fs-set__card fs-wa__setup" data-testid="whatsapp-setup-logged-out">
        <p className="fs-prose">{t('Unlinked — start again to get a new QR.')}</p>
        <Button variant="primary" label={t('Start the bridge')} loading={busy} onClick={() => void doStart()} testId="whatsapp-start" />
      </div>
    );
  }

  if (status.status === 'disconnected') {
    return (
      <div className="fs-set__card fs-wa__setup" data-testid="whatsapp-setup-disconnected">
        <p className="fs-prose">{t('Reconnecting…')}</p>
        {status.lastError && <p className="fs-set__help">{status.lastError}</p>}
      </div>
    );
  }

  // connected
  return (
    <div className="fs-set__card fs-wa__setup fs-wa__setup--connected" data-testid="whatsapp-setup-connected">
      <div className="fs-set__row-actions">
        {!confirmStop ? (
          <Button size="sm" variant="ghost" label={t('Stop')} disabled={busy} onClick={() => setConfirmStop(true)} testId="whatsapp-stop" />
        ) : (
          <span className="fs-modes__confirm" data-testid="whatsapp-stop-confirm">
            {t('Stop the bridge?')}
            <Button size="sm" variant="danger" label={t('Confirm')} loading={busy} onClick={() => void doStop()} />
            <Button size="sm" variant="ghost" label={t('Cancel')} disabled={busy} onClick={() => setConfirmStop(false)} />
          </span>
        )}
        {!confirmUnlink ? (
          <Button size="sm" variant="ghost" label={t('Unlink')} disabled={busy} onClick={() => setConfirmUnlink(true)} testId="whatsapp-unlink" />
        ) : (
          <span className="fs-modes__confirm" data-testid="whatsapp-unlink-confirm">
            {t('This unlinks the phone; you will need to scan again.')}
            <Button size="sm" variant="danger" label={t('Confirm')} loading={busy} onClick={() => void doUnlink()} />
            <Button size="sm" variant="ghost" label={t('Cancel')} disabled={busy} onClick={() => setConfirmUnlink(false)} />
          </span>
        )}
      </div>
    </div>
  );
}

/* ── Chat list ── */

function ChatListItem({ chat, active, onSelect }: { chat: WaChat; active: boolean; onSelect: () => void }) {
  return (
    <li>
      <button type="button" className="fs-wa__chat-item" data-active={active} onClick={onSelect} data-testid="whatsapp-chat-item">
        <span className="fs-wa__chat-item-head">
          <span className="fs-wa__chat-name">
            {chat.is_group && <Users size={12} aria-hidden="true" />} {chat.name || chat.jid}
          </span>
          {chat.last_ts != null && <span className="fs-set__help">{relativeTime(chat.last_ts)}</span>}
        </span>
        <span className="fs-wa__chat-item-body">
          <span className="fs-wa__chat-last">{chat.last_text}</span>
          {chat.unread > 0 && <span className="fs-wa__unread" data-testid="whatsapp-unread-badge">{chat.unread}</span>}
        </span>
      </button>
    </li>
  );
}

/* ── Message bubble ── */

function MessageBubble({ msg, showSender }: { msg: WaMessage; showSender: boolean }) {
  const isMedia = msg.kind !== 'text';
  return (
    <li className={`fs-wa__bubble-row${msg.from_me ? ' fs-wa__bubble-row--me' : ''}`} data-testid="whatsapp-message">
      <div className="fs-wa__bubble" data-mine={msg.from_me}>
        {showSender && !msg.from_me && <span className="fs-wa__bubble-sender">{msg.from_name}</span>}
        <span className={isMedia ? 'fs-set__help' : undefined}>{messageBody(msg)}</span>
        <span className="fs-wa__bubble-time">{formatTime(msg.ts)}</span>
      </div>
    </li>
  );
}

/* ── Composer ── */

function Composer({ chat, onSent, say }: { chat: string; onSent: () => void; say: (msg: string) => void }) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);

  const send = async () => {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    try {
      await waSend(chat, value);
      setText('');
      onSent();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fs-wa__composer">
      <textarea
        className="fs-wa__composer-input"
        value={text}
        placeholder={t('Write a message…')}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            void send();
          }
        }}
        rows={2}
        data-testid="whatsapp-composer-input"
      />
      <Button icon={Send} label={t('Send')} loading={busy} disabled={!text.trim()} onClick={() => void send()} testId="whatsapp-send" />
    </div>
  );
}

/* ── Chat pane (right side) ── */

function ChatPane({ chat, onBack }: { chat: WaChat; onBack: () => void }) {
  const [messages, setMessages] = useState<WaMessage[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | null>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const say = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  const reload = useCallback(() => {
    waMessages({ chat: chat.jid, hours: 48, limit: 200 })
      .then((m) => {
        setMessages(m);
        setFailed(false);
      })
      .catch((e) => {
        setFailed(true);
        say((e as Error).message);
      });
  }, [chat.jid, say]);

  useEffect(() => {
    setMessages(null);
    reload();
  }, [reload]);

  useEffect(() => {
    const id = window.setInterval(reload, 10000);
    return () => window.clearInterval(id);
  }, [reload]);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const markRead = async () => {
    try {
      await waMarkRead(chat.jid);
      reload();
    } catch (e) {
      say((e as Error).message);
    }
  };

  return (
    <div className="fs-wa__pane fs-wa__pane--chat" data-testid="whatsapp-chat-pane">
      <div className="fs-wa__pane-head">
        <Button size="sm" variant="ghost" icon={ArrowLeft} label={t('Back')} onClick={onBack} testId="whatsapp-back" />
        {/* Visible at any width; on desktop it simply clears the selection
            and returns to the list pane, which is fine there too — the
            two-pane layout below 900px is what needs it to escape the
            single-column chat view (see whatsapp.css). */}
        <strong>{chat.name || chat.jid}</strong>
        <Button size="sm" variant="ghost" label={t('Mark as read')} onClick={() => void markRead()} testId="whatsapp-mark-read" />
      </div>

      {messages === null ? (
        <Skeleton label={t('Loading')} count={3} height="48px" />
      ) : failed ? (
        <EmptyState title={t('Could not load messages.')} body={t('GET /api/whatsapp/messages failed.')} primaryAction={{ label: t('Try again'), onClick: reload }} />
      ) : messages.length === 0 ? (
        <EmptyState icon={MessageCircle} title={t('Nothing in the last 48 h')} body={t('No messages arrived in this chat during that window.')} />
      ) : (
        <ul className="fs-wa__bubbles" ref={listRef} data-testid="whatsapp-bubbles">
          {messages.map((m, i) => (
            <MessageBubble key={m.id} msg={m} showSender={chat.is_group && (i === 0 || messages[i - 1].from !== m.from)} />
          ))}
        </ul>
      )}

      <Composer chat={chat.jid} onSent={reload} say={say} />
      {notice && <Toast>{notice}</Toast>}
    </div>
  );
}

/* ── Screen ── */

export function WhatsAppScreen() {
  const [status, setStatus] = useState<WhatsAppStatus | null>(null);
  const [chats, setChats] = useState<WaChat[] | null>(null);
  const [selected, setSelected] = useState<WaChat | null>(null);
  const [filter, setFilter] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | null>(null);

  const say = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  const reloadStatus = useCallback(() => {
    waStatus()
      .then(setStatus)
      .catch((e) => say((e as Error).message));
  }, [say]);

  useEffect(() => {
    reloadStatus();
  }, [reloadStatus]);

  // Poll fast while nothing settled yet, slow once it has.
  useEffect(() => {
    if (!status) return;
    const fast = status.status === 'starting' || status.status === 'qr' || status.status === 'disconnected';
    const id = window.setInterval(reloadStatus, fast ? 2000 : 15000);
    return () => window.clearInterval(id);
  }, [status, reloadStatus]);

  const reloadChats = useCallback(() => {
    waChats(50)
      .then((c) => setChats(c))
      .catch((e) => say((e as Error).message));
  }, [say]);

  useEffect(() => {
    if (status?.status === 'connected') reloadChats();
  }, [status?.status, reloadChats]);

  useEffect(() => {
    if (status?.status !== 'connected') return;
    const id = window.setInterval(reloadChats, 15000);
    return () => window.clearInterval(id);
  }, [status?.status, reloadChats]);

  const filteredChats = useMemo(() => {
    if (!chats) return [];
    const q = filter.trim().toLowerCase();
    if (!q) return chats;
    return chats.filter((c) => (c.name || c.jid).toLowerCase().includes(q));
  }, [chats, filter]);

  const connected = status?.status === 'connected';

  return (
    <div className="fs-screen fs-wa" data-testid="whatsapp-screen">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('WhatsApp')}</h1>
          <p className="fs-prose">{t('Your own account through a local bridge: read what people wrote, answer from a chat, get a daily digest card.')}</p>
        </div>
        {status && <StatusPill status={status} />}
      </header>

      {status === null ? (
        <Skeleton label={t('Loading')} count={2} height="72px" />
      ) : !connected ? (
        <SetupPanel status={status} onChanged={reloadStatus} say={say} />
      ) : (
        <>
          <SetupPanel status={status} onChanged={reloadStatus} say={say} />
          <div className="fs-wa__panes" data-testid="whatsapp-panes" data-open={selected ? 'chat' : 'list'}>
            <div className="fs-wa__pane fs-wa__pane--list">
              <label className="fs-search" data-testid="whatsapp-filter">
                <input
                  type="search"
                  value={filter}
                  placeholder={t('Filter chats…')}
                  aria-label={t('Filter')}
                  onChange={(e) => setFilter(e.target.value)}
                />
              </label>
              {chats === null ? (
                <Skeleton label={t('Loading')} count={4} height="52px" />
              ) : filteredChats.length === 0 ? (
                <EmptyState icon={MessageCircle} title={t('No chats')} body={t('No chats match this filter yet.')} />
              ) : (
                <ul className="fs-wa__chat-list" data-testid="whatsapp-chat-list">
                  {filteredChats.map((c) => (
                    <ChatListItem key={c.jid} chat={c} active={selected?.jid === c.jid} onSelect={() => setSelected(c)} />
                  ))}
                </ul>
              )}
            </div>

            {selected ? (
              <ChatPane chat={selected} onBack={() => setSelected(null)} />
            ) : (
              <div className="fs-wa__pane fs-wa__pane--empty">
                <EmptyState icon={MessageCircle} title={t('Pick a chat')} body={t('Select a chat on the left to read and answer it.')} />
              </div>
            )}
          </div>
        </>
      )}

      {notice && <Toast>{notice}</Toast>}
    </div>
  );
}
