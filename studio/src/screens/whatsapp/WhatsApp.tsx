import { ArrowLeft, MessageCircle, Search, Users } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, EmptyState, IconButton, Skeleton, Toast } from '../../components';
import {
  waChats,
  waHistory,
  waLogout,
  waMarkRead,
  waMessages,
  waSearch,
  waStart,
  waStatus,
  waStop,
  waSubscribe,
  type WaChat,
  type WaMessage,
  type WhatsAppStatus,
} from '../../adapters/whatsapp';
import { relativeTime } from '../../adapters/home';
import { locale, t } from '../../i18n';
import { Avatar, MessageBubble, dayLabel } from './WhatsAppBubble';
import { Composer } from './WhatsAppComposer';
import { AskFaustus } from './WhatsAppAssist';
import '../projects.css';
import '../settings.css';
import '../connectors/connectors.css';
import './whatsapp.css';

/**
 * WhatsApp — Studio side of `routes/whatsapp_routes.py` +
 * `src/whatsapp_bridge.py`. A local Node bridge links the person's own
 * WhatsApp account (a linked device, exactly like WhatsApp Web): read what
 * people wrote, answer from a chat, get a daily digest card, and now a
 * WhatsApp-Web-like chat pane — replies, reactions, edits, delivery ticks,
 * live typing/presence, search, attachments and voice notes — plus "Ask
 * Faustus" to summarise, draft or translate without ever auto-sending.
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

const PRESENCE_LABEL: Record<string, string> = {
  composing: 'typing…',
  recording: 'recording audio…',
  available: 'online',
};

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

  // connected — a single compact row: title + status pill + Stop/Unlink.
  return (
    <div className="fs-wa__setup--connected" data-testid="whatsapp-setup-connected">
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
  );
}

/* ── Chat list ── */

function ChatListItem({ chat, active, onSelect }: { chat: WaChat; active: boolean; onSelect: () => void }) {
  return (
    <li>
      <button type="button" className="fs-wa__chat-item" data-active={active} onClick={onSelect} data-testid="whatsapp-chat-item">
        <Avatar jid={chat.jid} name={chat.name || chat.jid} size="list" />
        <span className="fs-wa__chat-item-main">
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
        </span>
      </button>
    </li>
  );
}

function MessageSearchResult({ msg, onOpen }: { msg: WaMessage; onOpen: (msg: WaMessage) => void }) {
  return (
    <li>
      <button type="button" className="fs-wa__search-result" onClick={() => onOpen(msg)} data-testid="whatsapp-search-result">
        <span className="fs-wa__chat-name">{msg.chat_name || msg.chat}</span>
        <span className="fs-wa__chat-last">{msg.text}</span>
      </button>
    </li>
  );
}

/* ── Chat pane (right side) ── */

function ChatPane({ chat, chats, onBack }: { chat: WaChat; chats: WaChat[]; onBack: () => void }) {
  const [messages, setMessages] = useState<WaMessage[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [transcripts, setTranscripts] = useState<Map<string, string>>(new Map());
  const [replyTo, setReplyTo] = useState<WaMessage | null>(null);
  const [composerText, setComposerText] = useState('');
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<WaMessage[] | null>(null);
  const noticeTimer = useRef<number | null>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const pendingScroll = useRef<number | null>(null);
  const olderTimers = useRef<number[]>([]);
  const searchTimer = useRef<number | null>(null);

  const say = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  const reload = useCallback(() => {
    const el = listRef.current;
    const nearBottom = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    const atTop = el ? el.scrollTop < 24 : false;
    const oldScrollHeight = el?.scrollHeight ?? 0;
    waMessages({ chat: chat.jid, hours: 24 * 365, limit: 300 })
      .then((m) => {
        setMessages(m);
        setFailed(false);
        if (atTop && el) {
          pendingScroll.current = oldScrollHeight;
        } else if (nearBottom) {
          pendingScroll.current = -1; // signal: scroll to bottom
        }
      })
      .catch((e) => {
        setFailed(true);
        say((e as Error).message);
      });
  }, [chat.jid, say]);

  useEffect(() => {
    setMessages(null);
    setTranscripts(new Map());
    setReplyTo(null);
    setSearchOpen(false);
    setSearchResults(null);
    setSearchQuery('');
    pendingScroll.current = -1; // scroll to bottom once the chat just opened
    reload();
    return () => {
      olderTimers.current.forEach((id) => window.clearTimeout(id));
      olderTimers.current = [];
    };
  }, [reload]);

  // Live presence: subscribe whenever a chat is opened.
  useEffect(() => {
    void waSubscribe(chat.jid).catch(() => {});
  }, [chat.jid]);

  // Real read receipts: once a chat with unread messages has been open a
  // moment, and only while the tab is actually visible.
  useEffect(() => {
    if (!chat.unread) return;
    if (typeof document !== 'undefined' && document.hidden) return;
    const id = window.setTimeout(() => {
      waMarkRead(chat.jid).catch(() => {});
    }, 1500);
    return () => window.clearTimeout(id);
  }, [chat.jid, chat.unread]);

  useEffect(() => {
    const id = window.setInterval(reload, 10000);
    return () => window.clearInterval(id);
  }, [reload]);

  useEffect(() => {
    const el = listRef.current;
    if (!el || pendingScroll.current === null) return;
    if (pendingScroll.current === -1) {
      el.scrollTop = el.scrollHeight;
    } else {
      const oldScrollHeight = pendingScroll.current;
      el.scrollTop += el.scrollHeight - oldScrollHeight;
    }
    pendingScroll.current = null;
  }, [messages]);

  const markRead = async () => {
    try {
      await waMarkRead(chat.jid);
      reload();
    } catch (e) {
      say((e as Error).message);
    }
  };

  const loadOlder = async () => {
    setLoadingOlder(true);
    say(t('Asking your phone…'));
    try {
      await waHistory(chat.jid, 100);
      const t1 = window.setTimeout(reload, 3000);
      const t2 = window.setTimeout(() => {
        reload();
        setLoadingOlder(false);
      }, 8000);
      olderTimers.current.push(t1, t2);
    } catch (e) {
      setLoadingOlder(false);
      say((e as Error).message);
    }
  };

  const onTranscribed = useCallback((id: string, text: string) => {
    setTranscripts((prev) => {
      const next = new Map(prev);
      next.set(id, text);
      return next;
    });
  }, []);

  const jumpTo = useCallback((id: string) => {
    const el = document.getElementById(`wa-msg-${id}`);
    if (el) {
      el.scrollIntoView({ block: 'center' });
      el.classList.add('fs-wa__bubble-row--flash');
      window.setTimeout(() => el.classList.remove('fs-wa__bubble-row--flash'), 1200);
    } else {
      say(t('That message is not loaded — try Load older messages.'));
    }
    setSearchOpen(false);
  }, [say]);

  const runSearch = useCallback((q: string) => {
    if (searchTimer.current) window.clearTimeout(searchTimer.current);
    if (!q.trim()) {
      setSearchResults(null);
      return;
    }
    searchTimer.current = window.setTimeout(() => {
      waSearch(q.trim(), chat.jid)
        .then(setSearchResults)
        .catch((e) => say((e as Error).message));
    }, 300);
  }, [chat.jid, say]);

  const presenceLine = chat.is_group ? null : (chat.presence && PRESENCE_LABEL[chat.presence] ? t(PRESENCE_LABEL[chat.presence]) : null);

  // Group messages with a date separator between two different days.
  const rows = useMemo(() => {
    if (!messages) return [];
    const out: { key: string; kind: 'sep' | 'msg'; label?: string; msg?: WaMessage; showSender?: boolean }[] = [];
    let lastDay = '';
    messages.forEach((m, i) => {
      const label = dayLabel(m.ts, locale());
      if (label !== lastDay) {
        out.push({ key: `sep-${m.id}`, kind: 'sep', label });
        lastDay = label;
      }
      out.push({ key: m.id, kind: 'msg', msg: m, showSender: chat.is_group && (i === 0 || messages[i - 1].from !== m.from) });
    });
    return out;
  }, [messages, chat.is_group]);

  return (
    <div className="fs-wa__pane fs-wa__pane--chat" data-testid="whatsapp-chat-pane">
      <div className="fs-wa__pane-head">
        <Button size="sm" variant="ghost" icon={ArrowLeft} label={t('Back')} onClick={onBack} testId="whatsapp-back" />
        {/* Visible at any width; on desktop it simply clears the selection
            and returns to the list pane, which is fine there too — the
            two-pane layout below 900px is what needs it to escape the
            single-column chat view (see whatsapp.css). */}
        <Avatar jid={chat.jid} name={chat.name || chat.jid} size="head" />
        <span className="fs-wa__pane-head-title">
          <strong>{chat.name || chat.jid}</strong>
          {presenceLine && <span className="fs-wa__presence" data-testid="whatsapp-presence">{presenceLine}</span>}
        </span>
        <AskFaustus chatJid={chat.jid} chatName={chat.name || chat.jid} onUseAsDraft={setComposerText} say={say} />
        <IconButton
          icon={Search}
          label={t('Search in this chat')}
          size="sm"
          onClick={() => setSearchOpen((v) => !v)}
          testId="whatsapp-search"
        />
        <Button size="sm" variant="ghost" label={t('Mark as read')} onClick={() => void markRead()} testId="whatsapp-mark-read" />
      </div>

      {searchOpen && (
        <div className="fs-wa__search-box">
          <input
            type="search"
            autoFocus
            value={searchQuery}
            placeholder={t('Search messages…')}
            aria-label={t('Search messages…')}
            onChange={(e) => { setSearchQuery(e.target.value); runSearch(e.target.value); }}
            data-testid="whatsapp-search-input"
          />
          {searchResults && (
            <ul className="fs-wa__search-results">
              {searchResults.length === 0 && <li className="fs-set__help">{t('No results.')}</li>}
              {searchResults.map((m) => (
                <MessageSearchResult key={m.id} msg={m} onOpen={(msg) => jumpTo(msg.id)} />
              ))}
            </ul>
          )}
        </div>
      )}

      {messages === null ? (
        <Skeleton label={t('Loading')} count={3} height="48px" />
      ) : failed ? (
        <EmptyState title={t('Could not load messages.')} body={t('GET /api/whatsapp/messages failed.')} primaryAction={{ label: t('Try again'), onClick: reload }} />
      ) : messages.length === 0 ? (
        <EmptyState icon={MessageCircle} title={t('No messages in this chat yet')} body={t('Nothing has arrived in this chat.')} />
      ) : (
        <ul className="fs-wa__bubbles" ref={listRef} data-testid="whatsapp-bubbles">
          <li className="fs-wa__load-older">
            <Button size="sm" variant="ghost" label={t('Load older messages')} loading={loadingOlder} onClick={() => void loadOlder()} testId="whatsapp-load-older" />
          </li>
          {rows.map((row) =>
            row.kind === 'sep' ? (
              <li key={row.key} className="fs-wa__day-sep"><span>{row.label}</span></li>
            ) : (
              <MessageBubble
                key={row.key}
                msg={row.msg as WaMessage}
                showSender={!!row.showSender}
                transcript={transcripts.get((row.msg as WaMessage).id)}
                onTranscribed={onTranscribed}
                say={say}
                chats={chats}
                onReply={setReplyTo}
                onChanged={reload}
                onJump={jumpTo}
              />
            ),
          )}
        </ul>
      )}

      <Composer
        chat={chat.jid}
        text={composerText}
        setText={setComposerText}
        replyTo={replyTo}
        onClearReply={() => setReplyTo(null)}
        onSent={reload}
        say={say}
      />
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
  const [messageHits, setMessageHits] = useState<WaMessage[] | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | null>(null);
  const globalSearchTimer = useRef<number | null>(null);

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
    waChats(300)
      .then((c) => setChats(c))
      .catch((e) => say((e as Error).message));
  }, [say]);

  useEffect(() => {
    if (status?.status === 'connected') reloadChats();
  }, [status?.status, reloadChats]);

  useEffect(() => {
    if (status?.status !== 'connected') return;
    const id = window.setInterval(reloadChats, 10000);
    return () => window.clearInterval(id);
  }, [status?.status, reloadChats]);

  // Global message search: once the filter is long enough, also look inside
  // every chat's text, not just chat names.
  useEffect(() => {
    if (globalSearchTimer.current) window.clearTimeout(globalSearchTimer.current);
    const q = filter.trim();
    if (q.length < 3) {
      setMessageHits(null);
      return;
    }
    globalSearchTimer.current = window.setTimeout(() => {
      waSearch(q)
        .then(setMessageHits)
        .catch((e) => say((e as Error).message));
    }, 300);
    return () => { if (globalSearchTimer.current) window.clearTimeout(globalSearchTimer.current); };
  }, [filter, say]);

  const filteredChats = useMemo(() => {
    if (!chats) return [];
    const q = filter.trim().toLowerCase();
    // Archived chats stay out (WhatsApp Web hides them too); without a filter
    // only chats that have said something are listed — the phone knows two
    // hundred dead groups, the person wants the live ones.
    const live = chats.filter((c) => !c.archived && (q || (c.last_ts ?? 0) > 0));
    if (!q) return live;
    return live.filter((c) => (c.name || c.jid).toLowerCase().includes(q));
  }, [chats, filter]);

  const openFromHit = useCallback((msg: WaMessage) => {
    const existing = chats?.find((c) => c.jid === msg.chat);
    setSelected(existing ?? { jid: msg.chat, name: msg.chat_name, is_group: msg.chat.endsWith('@g.us'), last_ts: msg.ts, last_text: msg.text, unread: 0 });
    setFilter('');
  }, [chats]);

  const connected = status?.status === 'connected';

  return (
    <div className="fs-screen fs-wa" data-testid="whatsapp-screen">
      <header className="fs-screen__head fs-wa__head">
        <div>
          <h1 className="fs-screen__title">{t('WhatsApp')}</h1>
          <p className="fs-prose">{t('Your own account through a local bridge: read what people wrote, answer from a chat, get a daily digest card.')}</p>
        </div>
        {status && <StatusPill status={status} />}
        {status && connected && <SetupPanel status={status} onChanged={reloadStatus} say={say} />}
      </header>

      {status === null ? (
        <Skeleton label={t('Loading')} count={2} height="72px" />
      ) : !connected ? (
        <SetupPanel status={status} onChanged={reloadStatus} say={say} />
      ) : (
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
            ) : filteredChats.length === 0 && !messageHits?.length ? (
              <EmptyState icon={MessageCircle} title={t('No chats')} body={t('No chats match this filter yet.')} />
            ) : (
              <>
                <ul className="fs-wa__chat-list" data-testid="whatsapp-chat-list">
                  {filteredChats.map((c) => (
                    <ChatListItem key={c.jid} chat={c} active={selected?.jid === c.jid} onSelect={() => setSelected(c)} />
                  ))}
                </ul>
                {messageHits && messageHits.length > 0 && (
                  <div className="fs-wa__message-hits" data-testid="whatsapp-message-hits">
                    <span className="fs-wa__hits-title">{t('Messages')}</span>
                    <ul className="fs-wa__chat-list">
                      {messageHits.slice(0, 20).map((m) => (
                        <MessageSearchResult key={m.id} msg={m} onOpen={openFromHit} />
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
          </div>

          {selected ? (
            <ChatPane chat={selected} chats={chats ?? []} onBack={() => setSelected(null)} />
          ) : (
            <div className="fs-wa__pane fs-wa__pane--empty">
              <EmptyState icon={MessageCircle} title={t('Pick a chat')} body={t('Select a chat on the left to read and answer it.')} />
            </div>
          )}
        </div>
      )}

      {notice && <Toast>{notice}</Toast>}
    </div>
  );
}
