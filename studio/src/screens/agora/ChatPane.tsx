import { ArrowDown, Check, Copy, CornerUpLeft, MoreHorizontal, Send, X } from 'lucide-react';
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import {
  addTask, editTask, getTask, readTarget, reopenThread, resolveThread, reviewTask, say as sendMessage,
  type AgoraGuild, type AgoraMessage, type AgoraPage, type AgoraTask, type AgoraThreadInfo, type Target,
} from '../../adapters/agora';
import { Button, IconButton, Menu, Skeleton, type MenuItem } from '../../components';
import { Rich } from '../rich';
import { t } from '../../i18n';
import {
  agentColor, agentInitials, clock, composerBus, dayLabel, findTask, isOnline, KIND_LABEL, STATUS_LABEL, stamp,
  THREAD_KIND_LABEL, type View,
} from './util';
import '../studio.css';

const GROUP_WINDOW_S = 7 * 60;
const LONG_BODY = 2400;
const KINDS = ['comment', 'proposal', 'agree', 'disagree'] as const;

type Item =
  | { type: 'day'; key: string; label: string }
  | { type: 'new'; key: string }
  | { type: 'msg'; key: string; message: AgoraMessage; head: boolean };

function buildItems(messages: AgoraMessage[], seenUpto: number, viewer: string): Item[] {
  const out: Item[] = [];
  let lastDay = '';
  let previous: AgoraMessage | null = null;
  let marked = false;
  for (const message of messages) {
    const day = new Date(message.created * 1000).toDateString();
    if (day !== lastDay) { out.push({ type: 'day', key: 'day-' + day, label: dayLabel(message.created) }); lastDay = day; previous = null; }
    if (!marked && seenUpto > 0 && message.id > seenUpto && message.author !== viewer && message.kind !== 'system') {
      out.push({ type: 'new', key: 'new-' + message.id });
      marked = true; previous = null;
    }
    const head = !previous || previous.author !== message.author || previous.kind === 'system' || message.kind !== 'comment'
      || previous.kind !== 'comment' || message.created - previous.created > GROUP_WINDOW_S;
    out.push({ type: 'msg', key: 'm-' + message.id, message, head: message.kind === 'system' ? false : head });
    previous = message;
  }
  return out;
}

const MessageRow = memo(function MessageRow({ message, head, viewer, guild, onOpen, onReply }: {
  message: AgoraMessage; head: boolean; viewer: string; guild: AgoraGuild; onOpen: (view: View) => void; onReply: (author: string) => void;
}) {
  const [whole, setWhole] = useState(false);
  const [copied, setCopied] = useState(false);
  if (message.kind === 'system') {
    return <div className="fs-agx-msg fs-agx-msg--system" data-testid="agora-message"><span>{message.body.length > 320 ? message.body.slice(0, 320) + '…' : message.body}</span><time>{clock(message.created)}</time></div>;
  }
  const mine = message.author === viewer;
  const toMe = message.mentions.includes(viewer);
  const long = message.body.length > LONG_BODY && !whole;
  const body = long ? message.body.slice(0, LONG_BODY) + '…' : message.body;
  const refs = [...new Set([...message.body.matchAll(/#(\d{1,6})\b/g)].map((m) => Number(m[1])))]
    .map((id) => findTask(guild, id)).filter((task): task is NonNullable<typeof task> => Boolean(task && task.thread_id)).slice(0, 5);
  const label = KIND_LABEL[message.kind] || '';
  const copy = () => { void navigator.clipboard?.writeText(message.body).then(() => { setCopied(true); window.setTimeout(() => setCopied(false), 1500); }).catch(() => undefined); };
  return (
    <article className="fs-agx-msg" data-head={head || undefined} data-mine={mine || undefined} data-tome={toMe || undefined}
      data-kind={message.kind} style={{ '--agx-c': agentColor(message.author) } as CSSProperties} data-testid="agora-message">
      {head
        ? <span className="fs-agx-avatar" aria-hidden="true">{agentInitials(message.author)}</span>
        : <time className="fs-agx-msg__gutter">{clock(message.created)}</time>}
      <div className="fs-agx-msg__main">
        {head && (
          <header className="fs-agx-msg__head">
            <strong>{message.author}</strong>
            {label && <span className="fs-agx-tag" data-kind={message.kind}>{t(label)}</span>}
            <time title={new Date(message.created * 1000).toLocaleString()}>{stamp(message.created)}</time>
            {message.mentions.length > 0 && <span className="fs-agx-msg__to">→ {message.mentions.slice(0, 6).map((m) => '@' + m).join(' ')}{message.mentions.length > 6 ? ' …' : ''}</span>}
          </header>
        )}
        <div className="fs-agx-msg__body"><Rich text={body} /></div>
        {long && <button type="button" className="fs-agx-link" onClick={() => setWhole(true)}>{t('Show the whole message ({n} characters)', { n: message.body.length })}</button>}
        {message.body_truncated && <p className="fs-muted">{t('This message is longer than what the page loads.')}</p>}
        {refs.length > 0 && (
          <div className="fs-agx-msg__refs">
            {refs.map((task) => (
              <button key={task.id} type="button" className="fs-agx-ref" data-status={task.status}
                onClick={() => onOpen({ kind: 'thread', id: String(task.thread_id) })} title={t(STATUS_LABEL[task.status] ?? task.status)}>
                #{task.id} {task.title}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="fs-agx-msg__tools">
        {!mine && <IconButton size="sm" icon={CornerUpLeft} label={t('Reply to {name}', { name: message.author })} onClick={() => onReply(message.author)} />}
        <IconButton size="sm" icon={copied ? Check : Copy} label={t('Copy the text')} onClick={copy} />
      </div>
    </article>
  );
});

interface Prompt { title: string; placeholder: string; confirm: string; run: (text: string) => Promise<unknown>; optional?: boolean }

function TaskBar({ task, guild, ask, act }: {
  task: AgoraTask; guild: AgoraGuild; ask: (prompt: Prompt) => void; act: (work: () => Promise<unknown>, done: string) => void;
}) {
  const space = guild.space.id;
  const fresh = async () => (await getTask(space, task.id)).task;
  const assignable = guild.members.filter((m) => m.role !== 'person' && isOnline(m)).slice(0, 14);
  const closed = task.status === 'done' || task.status === 'dropped';
  const items: (MenuItem | null)[] = [
    ...assignable.map((m) => ({ label: t('Assign to {name}', { name: m.id }), onSelect: () => act(async () => editTask(space, await fresh(), { owner: m.id }), t('Assigned to {name}', { name: m.id })) })),
    ...(task.owner ? [{ label: t('Leave it unassigned'), onSelect: () => act(async () => editTask(space, await fresh(), { owner: '' }), t('Unassigned')) }] : []),
    null,
    ...[0, 1, 2, 3].filter((p) => p !== task.priority).map((p) => ({ label: t('Priority {n}', { n: p }), onSelect: () => act(async () => editTask(space, await fresh(), { priority: p }), t('Priority changed')) })),
    null,
    ...(closed
      ? [{ label: t('Reopen'), onSelect: () => act(async () => editTask(space, await fresh(), { status: 'open' }), t('Open again')) }]
      : [
        ...(task.status !== 'review' && task.status !== 'changes' ? [{ label: t('Mark as done…'), onSelect: () => ask({ title: t('Close #{id} as done', { id: task.id }), placeholder: t('What was done (optional)'), confirm: t('Mark as done'), optional: true,
          run: async (text) => editTask(space, await fresh(), { status: 'done', ...(text ? { note: text } : {}) }) }) }] : []),
        { label: t('Drop it…'), variant: 'danger' as const, onSelect: () => ask({ title: t('Drop #{id}', { id: task.id }), placeholder: t('Why (optional)'), confirm: t('Drop it'), optional: true,
          run: async (text) => editTask(space, await fresh(), { status: 'dropped', ...(text ? { note: text } : {}) }) }) },
      ]),
  ];
  return (
    <div className="fs-agx-taskbar" data-testid="agora-taskbar">
      <span className="fs-agx-status" data-status={task.status}>{t(STATUS_LABEL[task.status] ?? task.status)}</span>
      <span className="fs-agx-taskbar__meta">
        {task.owner ? t('owner {name}', { name: task.owner }) : t('unassigned')}
        {task.reviewer ? ' · ' + t('reviewer {name}', { name: task.reviewer }) : ''}
        {' · P' + task.priority}{task.repo ? ' · ' + task.repo : ''}
        {task.waiting_on?.length ? ' · ' + t('waits for') + ' ' + task.waiting_on.map((i) => '#' + i).join(', ') : ''}
      </span>
      {task.commits?.length > 0 && <span className="fs-agx-taskbar__commits">{task.commits.slice(0, 4).map((c) => <code key={c}>{String(c).slice(0, 9)}</code>)}</span>}
      <span className="fs-spacer" />
      {(task.status === 'review' || task.status === 'approved' || task.status === 'changes') && task.owner !== guild.viewer && (
        <>
          <Button size="sm" variant="primary" label={t('Approve')} testId="agora-approve" onClick={() => ask({ title: t('Approve #{id}', { id: task.id }), placeholder: t('What you checked'), confirm: t('Approve'),
            run: async (text) => reviewTask(space, await fresh(), 'approve', text) })} />
          <Button size="sm" variant="secondary" label={t('Ask for changes')} testId="agora-changes" onClick={() => ask({ title: t('Ask for changes on #{id}', { id: task.id }), placeholder: t('What has to change'), confirm: t('Ask for changes'),
            run: async (text) => reviewTask(space, await fresh(), 'changes', text) })} />
        </>
      )}
      <Menu align="end" testId="agora-task-menu" items={items} trigger={<IconButton size="sm" icon={MoreHorizontal} label={t('Task actions')} />} />
    </div>
  );
}

function ThreadBar({ thread, guild, ask }: { thread: AgoraThreadInfo; guild: AgoraGuild; ask: (prompt: Prompt) => void }) {
  const space = guild.space.id;
  const waiting = guild.waiting.find((w) => w.id === thread.id);
  return (
    <div className="fs-agx-taskbar" data-testid="agora-threadbar">
      <span className="fs-agx-status" data-status={thread.status === 'escalated' ? 'blocked' : thread.status === 'resolved' ? 'done' : 'open'}>
        {t(THREAD_KIND_LABEL[thread.kind] ?? thread.kind)} · {thread.status === 'escalated' ? t('waiting for you') : thread.status === 'resolved' ? t('resolved') : t('open')}
      </span>
      <span className="fs-agx-taskbar__meta">
        {waiting?.question ? (waiting.escalated_by ? waiting.escalated_by + ': ' : '') + waiting.question.slice(0, 220) : thread.resolution ? t('Decision: {text}', { text: thread.resolution.slice(0, 220) }) : t('opened by {name}', { name: thread.created_by })}
      </span>
      <span className="fs-spacer" />
      {thread.status === 'resolved'
        ? <Button size="sm" variant="ghost" label={t('Reopen')} onClick={() => ask({ title: t('Reopen this thread'), placeholder: t('Why it needs another look'), confirm: t('Reopen'), run: (text) => reopenThread(space, thread.id, text) })} />
        : <Button size="sm" variant={thread.status === 'escalated' ? 'primary' : 'secondary'} label={t('Decide and close')} testId="agora-resolve"
            onClick={() => ask({ title: t('Decide and close'), placeholder: t('The decision, in writing: it goes to the decision log'), confirm: t('Decide and close'), run: (text) => resolveThread(space, thread.id, text) })} />}
    </div>
  );
}

export function ChatPane({ guild, view, target, version, onOpen, notify, placeholder }: {
  guild: AgoraGuild; view: View; target: Target; version: string; onOpen: (view: View) => void;
  notify: (text: string, tone?: 'ok' | 'warn') => void; placeholder: string;
}) {
  const space = guild.space.id;
  const viewer = guild.viewer || 'luis';
  const scope = space + '|' + view.kind + ':' + view.id;
  const [messages, setMessages] = useState<AgoraMessage[]>([]);
  const [info, setInfo] = useState<{ thread: AgoraThreadInfo | null; task: AgoraTask | null }>({ thread: null, task: null });
  const [seenUpto, setSeenUpto] = useState(0);
  const [more, setMore] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [older, setOlder] = useState(false);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState('');
  const [kind, setKind] = useState<(typeof KINDS)[number]>('comment');
  const [sending, setSending] = useState(false);
  const [prompt, setPrompt] = useState<Prompt | null>(null);
  const [promptText, setPromptText] = useState('');
  const [busy, setBusy] = useState(false);
  const [caret, setCaret] = useState(0);
  const [pick, setPick] = useState(0);
  const [below, setBelow] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const pinned = useRef(true);
  const jump = useRef<'bottom' | 'new' | null>('bottom');
  const active = useRef(scope);
  active.current = scope;
  const lastId = messages.length ? messages[messages.length - 1].id : 0;

  const apply = useCallback((page: AgoraPage, mode: 'reset' | 'merge') => {
    setInfo({ thread: page.thread, task: page.task ?? null });
    if (mode === 'reset') {
      setMessages(page.messages); setSeenUpto(page.seen_upto || 0);
      setMore(page.pagination.has_more ? page.pagination.next_before_id : null);
      jump.current = page.messages.some((m) => m.id > (page.seen_upto || 0) && m.author !== viewer && m.kind !== 'system') && page.seen_upto ? 'new' : 'bottom';
    } else {
      setMessages((old) => {
        const known = new Set(old.map((m) => m.id));
        const fresh = page.messages.filter((m) => !known.has(m.id));
        return fresh.length ? [...old, ...fresh].sort((a, b) => a.id - b.id) : old;
      });
    }
  }, [viewer]);

  // Open the conversation: the latest page, counted as read.
  useEffect(() => {
    let alive = true;
    setLoading(true); setError(''); setMessages([]); setInfo({ thread: null, task: null }); setMore(null); setPrompt(null); setKind('comment');
    try { setDraft(window.sessionStorage.getItem('agora:draft:' + scope) || ''); } catch { setDraft(''); }
    pinned.current = true;
    readTarget(space, target, { limit: 60 }).then((page) => { if (alive) apply(page, 'reset'); })
      .catch((e: Error) => { if (alive) setError(e.message); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope]);

  // Something changed in the Agora: bring what is new here.
  useEffect(() => {
    if (loading || !version) return;
    const mine = scope;
    readTarget(space, target, { limit: 40, peek: document.hidden }).then((page) => { if (active.current === mine) apply(page, 'merge'); }).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);

  useEffect(() => { try { window.sessionStorage.setItem('agora:draft:' + scope, draft); } catch { /* private mode */ } }, [draft, scope]);
  useEffect(() => composerBus.listen((text) => {
    setDraft((old) => (old && !old.endsWith(' ') && !old.endsWith('\n') ? old + ' ' : old) + text);
    input.current?.focus();
  }), []);

  useLayoutEffect(() => {
    const box = scroller.current;
    if (!box) return;
    if (jump.current === 'new') {
      const mark = box.querySelector('.fs-agx-new');
      if (mark) (mark as HTMLElement).scrollIntoView({ block: 'center' }); else box.scrollTop = box.scrollHeight;
      jump.current = null;
    } else if (jump.current === 'bottom' || pinned.current) {
      box.scrollTop = box.scrollHeight; jump.current = null;
    }
  }, [messages, loading]);

  const onScroll = () => {
    const box = scroller.current;
    if (!box) return;
    const gap = box.scrollHeight - box.scrollTop - box.clientHeight;
    pinned.current = gap < 80;
    setBelow(gap > 400);
  };
  const toBottom = () => { const box = scroller.current; if (box) { box.scrollTop = box.scrollHeight; pinned.current = true; setBelow(false); } };

  const loadOlder = async () => {
    if (!more || older) return;
    const box = scroller.current;
    const height = box?.scrollHeight ?? 0;
    setOlder(true);
    try {
      const page = await readTarget(space, target, { limit: 60, before: more, peek: true });
      setMessages((old) => [...page.messages, ...old]);
      setMore(page.pagination.has_more ? page.pagination.next_before_id : null);
      window.requestAnimationFrame(() => { if (box) box.scrollTop = box.scrollHeight - height; });
    } catch (e) { notify((e as Error).message, 'warn'); }
    finally { setOlder(false); }
  };

  const reload = useCallback(async () => {
    try { apply(await readTarget(space, target, { limit: 40 }), 'merge'); } catch { /* the next pulse brings it */ }
  }, [apply, space, target]);

  const act = useCallback((work: () => Promise<unknown>, done: string) => {
    setBusy(true);
    void work().then(() => { notify(done); return reload(); }).catch((e: Error) => notify(e.message, 'warn')).finally(() => setBusy(false));
  }, [notify, reload]);

  const members = guild.members;
  const mention = useMemo(() => {
    const before = draft.slice(0, caret);
    const hit = /(^|\s)@([a-zA-Z0-9_.-]*)$/.exec(before);
    if (!hit) return null;
    const typed = hit[2].toLowerCase();
    const ids = [...members.filter((m) => isOnline(m) && m.id !== viewer).map((m) => m.id), ...members.filter((m) => !isOnline(m)).map((m) => m.id), 'here'];
    const options = [...new Set(ids)].filter((id) => id.startsWith(typed)).slice(0, 8);
    return options.length ? { start: before.length - typed.length - 1, typed, options } : null;
  }, [draft, caret, members, viewer]);
  useEffect(() => { setPick(0); }, [mention?.typed]);

  const choose = (id: string) => {
    if (!mention) return;
    const next = draft.slice(0, mention.start) + '@' + id + ' ' + draft.slice(caret);
    setDraft(next);
    const at = mention.start + id.length + 2;
    window.requestAnimationFrame(() => { input.current?.focus(); input.current?.setSelectionRange(at, at); setCaret(at); });
  };

  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      const command = /^\/(tarea|task)\s+(.+)/is.exec(text);
      const decide = /^\/(decidir|decide)\s+(.+)/is.exec(text);
      if (command) {
        const made = await addTask(space, { title: command[2].split('\n')[0].slice(0, 300), body: command[2].split('\n').slice(1).join('\n').trim() });
        notify(t('Task #{id} created', { id: made.task.id }));
        setDraft('');
        if (made.task.thread_id) onOpen({ kind: 'thread', id: String(made.task.thread_id) });
      } else if (decide && info.thread && !info.task) {
        await resolveThread(space, info.thread.id, decide[2]);
        notify(t('Decision recorded')); setDraft(''); await reload();
      } else {
        const sent = await sendMessage(space, target, text, kind);
        setDraft(''); setKind('comment'); pinned.current = true;
        if (sent.mentions.length) notify(t('Mentioned: {names}', { names: sent.mentions.slice(0, 8).join(', ') }));
        await reload();
      }
    } catch (e) { notify((e as Error).message, 'warn'); }
    finally { setSending(false); input.current?.focus(); }
  };

  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (mention) {
      if (event.key === 'ArrowDown') { event.preventDefault(); setPick((p) => (p + 1) % mention.options.length); return; }
      if (event.key === 'ArrowUp') { event.preventDefault(); setPick((p) => (p - 1 + mention.options.length) % mention.options.length); return; }
      if (event.key === 'Enter' || event.key === 'Tab') { event.preventDefault(); choose(mention.options[pick] ?? mention.options[0]); return; }
      if (event.key === 'Escape') { setCaret(0); return; }
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); }
  };

  useLayoutEffect(() => {
    const area = input.current;
    if (!area) return;
    area.style.blockSize = 'auto';
    area.style.blockSize = Math.min(220, Math.max(44, area.scrollHeight)) + 'px';
  }, [draft]);

  const items = useMemo(() => buildItems(messages, seenUpto, viewer), [messages, seenUpto, viewer]);
  const reply = useCallback((author: string) => { composerBus.insert('@' + author + ' '); }, []);
  const debate = info.thread && !info.task && ['debate', 'question', 'decision', 'review'].includes(info.thread.kind);
  const runPrompt = async () => {
    if (!prompt || (!prompt.optional && !promptText.trim())) return;
    setBusy(true);
    try { await prompt.run(promptText.trim()); notify(prompt.confirm + ' ✓'); setPrompt(null); setPromptText(''); await reload(); }
    catch (e) { notify((e as Error).message, 'warn'); }
    finally { setBusy(false); }
  };

  return (
    <section className="fs-agx-chat" data-testid="agora-chat" aria-busy={busy || undefined}>
      {info.task && <TaskBar task={info.task} guild={guild} ask={(p) => { setPrompt(p); setPromptText(''); }} act={act} />}
      {!info.task && info.thread && view.kind === 'thread' && <ThreadBar thread={info.thread} guild={guild} ask={(p) => { setPrompt(p); setPromptText(''); }} />}
      <div className="fs-agx-chat__scroll" ref={scroller} onScroll={onScroll} role="log" aria-live="polite" aria-label={t('Messages')}>
        {loading && <Skeleton label={t('Loading the conversation')} count={6} height="44px" />}
        {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
        {!loading && !error && more !== null && (
          <div className="fs-agx-chat__older"><Button size="sm" variant="ghost" label={t('Load earlier messages')} loading={older} onClick={() => void loadOlder()} testId="agora-older" /></div>
        )}
        {!loading && !error && !messages.length && (
          <div className="fs-agx-chat__empty">
            <h3>{view.kind === 'channel' ? '#' + view.id : info.thread?.title}</h3>
            <p>{t('Nothing has been said here yet. Write the first message: agents read it on their next turn, and a mention queues it for them.')}</p>
          </div>
        )}
        {items.map((item) => item.type === 'day'
          ? <div key={item.key} className="fs-agx-day"><span>{item.label}</span></div>
          : item.type === 'new'
            ? <div key={item.key} className="fs-agx-new"><span>{t('New')}</span></div>
            : <MessageRow key={item.key} message={item.message} head={item.head} viewer={viewer} guild={guild} onOpen={onOpen} onReply={reply} />)}
      </div>
      {below && <button type="button" className="fs-agx-chat__jump" onClick={toBottom}><ArrowDown size={14} aria-hidden="true" /> {t('Latest messages')}</button>}
      {prompt ? (
        <form className="fs-agx-prompt" onSubmit={(event) => { event.preventDefault(); void runPrompt(); }} data-testid="agora-prompt">
          <header><strong>{prompt.title}</strong><IconButton size="sm" icon={X} label={t('Cancel')} onClick={() => setPrompt(null)} /></header>
          <textarea autoFocus value={promptText} placeholder={prompt.placeholder} onChange={(e) => setPromptText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void runPrompt(); } if (e.key === 'Escape') setPrompt(null); }} />
          <footer><span className="fs-muted">{t('Ctrl + Enter confirms')}</span><Button size="sm" variant="primary" type="submit" label={prompt.confirm} loading={busy} disabled={!prompt.optional && !promptText.trim()} /></footer>
        </form>
      ) : (
        <div className="fs-agx-composer" data-testid="agora-composer">
          {mention && (
            <ul className="fs-agx-mentions" role="listbox" aria-label={t('Mention')}>
              {mention.options.map((id, index) => {
                const member = members.find((m) => m.id === id);
                return (
                  <li key={id}>
                    <button type="button" role="option" aria-selected={index === pick} onMouseDown={(e) => { e.preventDefault(); choose(id); }}>
                      <span className="fs-agx-mini" style={{ '--agx-c': agentColor(id) } as CSSProperties}>{id === 'here' ? '@' : agentInitials(id)}</span>
                      <strong>{id}</strong>
                      <span className="fs-muted">{id === 'here' ? t('everyone around lately') : member?.doing || (member ? t(member.presence === 'offline' ? 'Offline' : 'Around') : '')}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {debate && (
            <div className="fs-seg fs-agx-composer__kind" role="radiogroup" aria-label={t('Kind of message')}>
              {KINDS.map((k) => <button key={k} type="button" role="radio" aria-checked={kind === k} onClick={() => setKind(k)}>{t(k === 'comment' ? 'Plain comment' : KIND_LABEL[k])}</button>)}
            </div>
          )}
          <div className="fs-agx-composer__box">
            <textarea ref={input} value={draft} rows={1} maxLength={20000} placeholder={placeholder} aria-label={placeholder}
              onChange={(e) => { setDraft(e.target.value); setCaret(e.target.selectionStart ?? e.target.value.length); }}
              onSelect={(e) => setCaret((e.target as HTMLTextAreaElement).selectionStart ?? 0)} onKeyDown={onKey} data-testid="agora-input" />
            <IconButton icon={Send} label={t('Send')} disabled={!draft.trim() || sending} onClick={() => void send()} testId="agora-send" />
          </div>
          <p className="fs-agx-composer__hint">{t('Enter sends · Shift + Enter new line · @ mentions · /tarea Title creates a task')}</p>
        </div>
      )}
    </section>
  );
}
