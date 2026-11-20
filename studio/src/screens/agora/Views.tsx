import { ArrowRight, FolderOpen, FolderPlus, Inbox, Lock, MessageCircleQuestion, MessagesSquare, MoreHorizontal, Plus, Search, Trash2, X } from 'lucide-react';
import { useContext, useEffect, useRef, useState, type CSSProperties, type DragEvent } from 'react';
import {
  BOARD_COLUMNS, editTask, getFeed, getInbox, getLocks, getTask, searchAgora, setSpaceMeta,
  type ActivityItem, type AgoraGuild, type AgoraLock, type AgoraServer, type AgoraTaskRow, type BoardColumn, type FeedItem,
  type PersonInbox, type SearchHit,
} from '../../adapters/agora';
import { pickNative } from '../../adapters/composer';
import { Button, EmptyState, IconButton, Menu, Skeleton } from '../../components';
import { Rich } from '../rich';
import { CountBadge } from './ChannelList';
import { PendingProduction, ProductionHistory, readDecisionLog, type PendingProductionState } from './ProductionDecisions';
import { t, tn } from '../../i18n';
import { AgentAvatar, AgentFace } from './Identity';
import { LiveSteps } from './Work';
import { ago, agentColor, AgoraPersonContext, COLUMN_LABEL, guildPerson, STATUS_LABEL, stamp, THREAD_KIND_LABEL, type View } from './util';
import '../studio.css';

type Open = (view: View, space?: string) => void;
type Notify = (text: string, tone?: 'ok' | 'warn') => void;

function Who({ id }: { id: string | null | undefined }) {
  const person = useContext(AgoraPersonContext);
  if (!id) return null;
  return <span className="fs-agx-mini" style={{ '--agx-c': agentColor(id, person?.id) } as CSSProperties} title={id}><AgentFace id={id} size={11} /></span>;
}

/* ── Board ── */

const DROP_STATUS: Partial<Record<BoardColumn, string>> = { open: 'open', in_progress: 'in_progress', done: 'done' };

export function BoardView({ guild, onOpen, onNewTask, notify }: { guild: AgoraGuild; onOpen: Open; onNewTask: () => void; notify: Notify }) {
  const [over, setOver] = useState<BoardColumn | null>(null);
  const [filter, setFilter] = useState('');
  const needle = filter.trim().toLowerCase();
  // The minutes of the steps on the cards keep counting between two reads of the board.
  const [now, setNow] = useState(() => Date.now());
  const working = guild.tasks.in_progress?.some((task) => task.live);
  useEffect(() => {
    if (!working) return;
    const timer = window.setInterval(() => setNow(Date.now()), 15000);
    return () => window.clearInterval(timer);
  }, [working]);
  const match = (task: AgoraTaskRow) => !needle || `#${task.id} ${task.title} ${task.owner ?? ''} ${task.kind} ${task.repo ?? ''}`.toLowerCase().includes(needle);
  const drop = (event: DragEvent, column: BoardColumn) => {
    event.preventDefault(); setOver(null);
    return move(Number(event.dataTransfer.getData('text/agora-task')), column);
  };
  // Dragging and the card's own menu do the same thing, so the board is not for the mouse only.
  const move = async (id: number, column: BoardColumn) => {
    const status = DROP_STATUS[column];
    if (!id) return;
    if (!status) { notify(t('A task enters review when its owner submits it.'), 'warn'); return; }
    try {
      const { task } = await getTask(guild.space.id, id);
      if (task.status === status) return;
      await editTask(guild.space.id, task, { status });
      notify(t('#{id} moved to {column}', { id, column: t(COLUMN_LABEL[column]) }));
    } catch (e) { notify((e as Error).message, 'warn'); }
  };
  return (
    <div className="fs-agx-board" data-testid="agora-board">
      <div className="fs-agx-board__bar">
        <input className="fs-field" placeholder={t('Filter by title, owner or kind')} aria-label={t('Filter tasks')} value={filter} onChange={(e) => setFilter(e.target.value)} />
        <span className="fs-muted">{t('Drag a card to To do, In progress or Done; review goes through its owner.')}</span>
        <span className="fs-spacer" />
        <Button size="sm" variant="primary" icon={Plus} label={t('New task')} onClick={onNewTask} testId="agora-board-new" />
      </div>
      <div className="fs-agx-board__cols">
        {BOARD_COLUMNS.map((column) => {
          const tasks = (guild.tasks[column] ?? []).filter(match);
          const total = guild.column_counts[column] ?? tasks.length;
          return (
            <section key={column} className="fs-agx-col" data-column={column} data-over={over === column || undefined}
              onDragOver={(e) => { e.preventDefault(); setOver(column); }} onDragLeave={() => setOver((c) => (c === column ? null : c))} onDrop={(e) => void drop(e, column)}>
              <header><h3>{t(COLUMN_LABEL[column])}</h3><span className="fs-agx-cat__count">{needle ? tasks.length : total}</span></header>
              <ul>
                {tasks.map((task) => (
                  <li key={task.id} className="fs-agx-card" data-status={task.status} data-unread={task.unread > 0 || undefined}>
                    <button type="button" className="fs-agx-card__open" draggable
                      onDragStart={(e) => { e.dataTransfer.setData('text/agora-task', String(task.id)); e.dataTransfer.effectAllowed = 'move'; }}
                      onClick={() => task.thread_id && onOpen({ kind: 'thread', id: String(task.thread_id) })} data-testid={'agora-card-' + task.id}>
                      <span className="fs-agx-card__top"><span className="fs-agx-card__id">#{task.id}</span>
                        <span className="fs-agx-tag" data-kind={task.kind}>{task.kind}</span>
                        {task.priority <= 1 && <span className="fs-agx-tag" data-kind="prio">P{task.priority}</span>}
                        {task.status !== column && <span className="fs-agx-status" data-status={task.status}>{t(STATUS_LABEL[task.status] ?? task.status)}</span>}
                        {task.mentions > 0 ? <CountBadge n={task.mentions} mentions /> : task.unread > 0 ? <CountBadge n={task.unread} /> : null}
                      </span>
                      <strong>{task.title}</strong>
                      <LiveSteps live={task.live} now={now} />
                      <span className="fs-agx-card__foot"><Who id={task.owner} /><span>{task.owner ?? t('unassigned')}</span>
                        {task.waiting_on.length > 0 && <span className="fs-agx-card__wait">{t('waits for')} {task.waiting_on.map((i) => '#' + i).join(', ')}</span>}
                        <time>{ago(task.updated)}</time></span>
                    </button>
                    <span className="fs-agx-card__menu">
                      <Menu align="end" testId={'agora-card-menu-' + task.id}
                        items={(Object.keys(DROP_STATUS) as BoardColumn[]).filter((to) => to !== column).map((to) => (
                          { label: t('Move to {column}', { column: t(COLUMN_LABEL[to]) }), icon: ArrowRight, onSelect: () => void move(task.id, to) }))}
                        trigger={<IconButton size="sm" icon={MoreHorizontal} label={t('Move #{id}…', { id: task.id })} testId={'agora-card-move-' + task.id} />} />
                    </span>
                  </li>
                ))}
                {!tasks.length && <li className="fs-agx-cat__empty">{t('Nothing here.')}</li>}
                {!needle && total > tasks.length && <li className="fs-agx-cat__empty">{t('Showing the {n} most recent of {total}.', { n: tasks.length, total })}</li>}
              </ul>
            </section>
          );
        })}
      </div>
    </div>
  );
}

/* ── Feeds ── */

/**
 * One feed of a server. A change in the Agora brings the first page again; if the reader had asked for earlier
 * pages they stay, with the fresh page on top. Nothing of another server or feed is shown while the new one loads,
 * and a page that arrives late for a feed the reader already left is dropped.
 */
function useFeed<T extends { id: number }>(space: string, kind: 'deliveries' | 'reviews' | 'activity', version: string) {
  const scope = space + ':' + kind;
  const [state, setState] = useState<{ scope: string; items: T[]; next: string | number | null; extended: boolean } | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const current = useRef(scope);
  current.current = scope;
  useEffect(() => {
    const controller = new AbortController();
    getFeed<T>(space, kind, null, controller.signal).then((page) => {
      setError('');
      setState((old) => {
        if (!old || old.scope !== scope || !old.extended) return { scope, items: page.items, next: page.next_before, extended: false };
        const fresh = new Set(page.items.map((item) => item.id));
        return { ...old, items: [...page.items, ...old.items.filter((item) => !fresh.has(item.id))] };
      });
    }).catch((e: Error) => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [space, kind, scope, version]);
  const mine = state && state.scope === scope ? state : null;
  const more = async () => {
    if (!mine?.next || busy) return;
    setBusy(true);
    try {
      const page = await getFeed<T>(space, kind, mine.next);
      if (current.current !== scope) return;
      setState((old) => {
        if (!old || old.scope !== scope) return old;
        const known = new Set(old.items.map((item) => item.id));
        return { scope, items: [...old.items, ...page.items.filter((item) => !known.has(item.id))], next: page.next_before, extended: true };
      });
    } catch (e) { if (current.current === scope) setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return { items: mine ? mine.items : null, next: mine ? mine.next : null, error, busy, more };
}

export function DeliveriesView({ guild, version, onOpen, reviews = false }: { guild: AgoraGuild; version: string; onOpen: Open; reviews?: boolean }) {
  const feed = useFeed<FeedItem>(guild.space.id, reviews ? 'reviews' : 'deliveries', version);
  if (feed.error) return <p className="fs-notice" data-tone="danger" role="alert">{feed.error}</p>;
  if (!feed.items) return <Skeleton label={t('Loading deliveries')} count={5} height="72px" />;
  if (!feed.items.length) {
    return <EmptyState title={reviews ? t('Nothing waits for review') : t('No deliveries yet')}
      body={reviews ? t('When an agent submits a task, it shows up here with its commits so you or another agent can review it.') : t('Tasks sent to review, approved or closed appear here with their commits and result.')} />;
  }
  return (
    <div className="fs-agx-feed" data-testid={reviews ? 'agora-reviews' : 'agora-deliveries'}>
      {feed.items.map((item) => (
        <article key={item.id + ':' + item.at} className="fs-agx-delivery" data-status={item.status}>
          <header>
            <Who id={item.owner} />
            <button type="button" className="fs-agx-delivery__title" onClick={() => item.thread_id && onOpen({ kind: 'thread', id: String(item.thread_id) })}>
              #{item.id} {item.title}
            </button>
            <span className="fs-agx-status" data-status={item.status}>{t(STATUS_LABEL[item.status] ?? item.status)}</span>
            <time>{stamp(item.at)}</time>
          </header>
          <p className="fs-agx-delivery__meta">
            {item.owner ?? t('unassigned')}{item.reviewer ? ' → ' + t('reviewer {name}', { name: item.reviewer }) : ''}
            {item.repo ? ' · ' + item.repo : ''}{item.branch ? ' · ' + item.branch : ''}
            {item.review_state && item.status === 'done' ? ' · ' + t(item.review_state === 'approved' ? 'reviewed' : item.review_state === 'exempt' ? 'review exempt' : item.review_state === 'equivalent' ? 'integrated as equivalent' : 'unreviewed') : ''}
          </p>
          {item.commits.length > 0 && <p className="fs-agx-delivery__commits">{item.commits.slice(0, 8).map((c) => <code key={c}>{String(c).slice(0, 10)}</code>)}{item.commits.length > 8 ? ' +' + (item.commits.length - 8) : ''}</p>}
          {item.summary && <div className="fs-agx-delivery__body"><Rich text={item.summary + (item.summary_truncated ? '…' : '')} /></div>}
        </article>
      ))}
      {feed.next && <Button size="sm" variant="ghost" label={t('Earlier')} loading={feed.busy} onClick={() => void feed.more()} />}
    </div>
  );
}

export function ActivityView({ guild, version, onOpen }: { guild: AgoraGuild; version: string; onOpen: Open }) {
  const feed = useFeed<ActivityItem>(guild.space.id, 'activity', version);
  const person = guildPerson(guild);
  if (feed.error) return <p className="fs-notice" data-tone="danger" role="alert">{feed.error}</p>;
  if (!feed.items) return <Skeleton label={t('Loading activity')} count={6} height="48px" />;
  if (!feed.items.length) return <EmptyState title={t('No activity yet')} body={t('Every message of this server, newest first, with the thread it belongs to.')} />;
  return (
    <div className="fs-agx-feed" data-testid="agora-activity">
      {feed.items.map((item) => (
        <article key={item.id} className="fs-agx-activity" style={{ '--agx-c': agentColor(item.author, person?.id) } as CSSProperties}>
          <AgentAvatar id={item.author} />
          <div>
            <header><strong>{item.author === person?.id ? person.name : item.author}</strong>
              <button type="button" className="fs-agx-link" onClick={() => onOpen({ kind: 'thread', id: String(item.thread_id) })}>
                {item.task_id ? '#' + item.task_id + ' ' : t(THREAD_KIND_LABEL[item.thread_kind] ?? item.thread_kind) + ' · '}{item.thread_title}
              </button>
              <time>{stamp(item.created)}</time></header>
            <p>{item.body.length > 420 ? item.body.slice(0, 420) + '…' : item.body}</p>
          </div>
        </article>
      ))}
      {feed.next && <Button size="sm" variant="ghost" label={t('Earlier')} loading={feed.busy} onClick={() => void feed.more()} />}
    </div>
  );
}

export function DecisionsView({ guild, version, onOpen, production }: {
  guild: AgoraGuild; version: string; onOpen: Open; production?: PendingProductionState;
}) {
  const [log, setLog] = useState<Awaited<ReturnType<typeof readDecisionLog>> | null>(null);
  const rows = log?.threads;
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let revision = 0;
    const load = () => {
      const current = ++revision;
      const active = () => !controller.signal.aborted && current === revision;
      void readDecisionLog(guild.space.id, controller.signal).then((next) => {
        if (active()) { setLog(next); setError(''); }
      }).catch((e: Error) => { if (active()) setError(e.message); });
    };
    const onVisibility = () => { if (!document.hidden) load(); };
    load();
    document.addEventListener('visibilitychange', onVisibility);
    return () => { controller.abort(); document.removeEventListener('visibilitychange', onVisibility); };
  }, [guild.space.id, version]);
  const empty = production && !production.pending.length && !production.pendingError && log && !log.production.length && !log.productionError && rows?.length === 0 && !error;
  return (
    <>
      <PendingProduction state={production} />
      {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      {(!production || (!rows && !error)) && <Skeleton label={t('Loading decisions')} count={2} height="64px" />}
      {empty && <EmptyState title={t('No decisions yet')} body={t('A debate or question closed with its resolution in writing lands here: the decision log of this server.')} />}
      {(!!rows?.length || !!log?.production.length || log?.productionError) && (
        <section className="fs-agx-production" aria-labelledby="agora-decision-log">
          <h2 id="agora-decision-log">{t('Decision log')}</h2>
          {log && <ProductionHistory rows={log.production} error={log.productionError} />}
          {!!rows?.length && <section aria-labelledby="agora-closed-debates">
            <h3 id="agora-closed-debates">{t('Resolved debates and questions')}</h3>
            <div className="fs-agx-feed" data-testid="agora-decisions">
              {rows.map((row) => (
                <article key={row.id} className="fs-agx-delivery" data-status="done">
                  <header>
                    <button type="button" className="fs-agx-delivery__title" onClick={() => onOpen({ kind: 'thread', id: String(row.id) })}>{row.title}</button>
                    <span className="fs-agx-tag">{t(THREAD_KIND_LABEL[row.kind] ?? row.kind)}</span>
                    <time>{stamp(row.updated)}</time>
                  </header>
                  <div className="fs-agx-delivery__body"><Rich text={row.resolution || ''} /></div>
                  <p className="fs-agx-delivery__meta">{t('decided by {name}', { name: row.resolved_by || '—' })}</p>
                </article>
              ))}
            </div>
          </section>}
        </section>
      )}
    </>
  );
}

export function LocksView({ version }: { version: string }) {
  const [rows, setRows] = useState<AgoraLock[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    getLocks(controller.signal).then((r) => { setRows(r.locks); setError(''); }).catch((e: Error) => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [version]);
  if (error) return <p className="fs-notice" data-tone="danger" role="alert">{error}</p>;
  if (!rows) return <Skeleton label={t('Loading locks')} count={4} height="36px" />;
  if (!rows.length) return <EmptyState icon={Lock} title={t('Nothing is locked')} body={t('When an agent claims a task it takes its files, a repository merge, a model or an app; the leases show up here.')} />;
  const byOwner = new Map<string, AgoraLock[]>();
  for (const row of rows) byOwner.set(row.owner, [...(byOwner.get(row.owner) ?? []), row]);
  return (
    <div className="fs-agx-feed" data-testid="agora-locks">
      <p className="fs-muted">{t('Leases are shared by every server: files, merges, models and apps taken right now.')}</p>
      {[...byOwner.entries()].map(([owner, locks]) => (
        <article key={owner} className="fs-agx-delivery">
          <header><Who id={owner} /><strong>{owner}</strong><span className="fs-agx-cat__count">{locks.length}</span></header>
          <ul className="fs-agx-locks">
            {locks.slice(0, 40).map((lock) => (
              <li key={lock.resource}><code>{lock.resource}</code>{lock.task_id ? <span className="fs-muted"> #{lock.task_id}</span> : null}
                <time>{t('until {time}', { time: stamp(lock.expires) })}</time></li>
            ))}
            {locks.length > 40 && <li className="fs-muted">{tn(locks.length - 40, '{n} more', '{n} more')}</li>}
          </ul>
        </article>
      ))}
    </div>
  );
}

/* ── Inbox: what waits for the person in every server ── */

export function InboxView({ servers, version, onOpen }: { servers: AgoraServer[]; version: string; onOpen: Open }) {
  const [inbox, setInbox] = useState<PersonInbox | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    getInbox(controller.signal).then((r) => { setInbox(r); setError(''); }).catch((e: Error) => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [version]);
  const name = (id: string) => servers.find((s) => s.id === id)?.title ?? id;
  if (error) return <p className="fs-notice" data-tone="danger" role="alert">{error}</p>;
  if (!inbox) return <Skeleton label={t('Loading your inbox')} count={5} height="56px" />;
  const empty = !inbox.escalated.length && !inbox.pending.length && !inbox.reviews.length;
  if (empty) return <EmptyState icon={Inbox} title={t('Nothing waits for you')} body={t('Questions the agents escalate to you, messages that mention you and submissions without a reviewer show up here, from every server.')} />;
  return (
    <div className="fs-agx-feed" data-testid="agora-inbox">
      {inbox.escalated.length > 0 && <h3 className="fs-agx-feed__title">{t('Waiting for you')} <span className="fs-agx-cat__count">{inbox.escalated.length}</span></h3>}
      {inbox.escalated.map((thread) => (
        <article key={'e' + thread.id} className="fs-agx-delivery" data-status="changes">
          <header><Who id={thread.escalated_by} />
            <button type="button" className="fs-agx-delivery__title" onClick={() => onOpen({ kind: 'thread', id: String(thread.id) }, thread.space_id)}>{thread.title}</button>
            <span className="fs-agx-tag">{name(thread.space_id)}</span><time>{stamp(thread.updated)}</time></header>
          {thread.question && <p className="fs-agx-delivery__meta">{thread.escalated_by ? thread.escalated_by + ': ' : ''}{thread.question.slice(0, 600)}</p>}
        </article>
      ))}
      {inbox.pending.length > 0 && <h3 className="fs-agx-feed__title">{t('They mention you')} <span className="fs-agx-cat__count">{inbox.pending.length}</span></h3>}
      {inbox.pending.map((message) => (
        <article key={'m' + message.id} className="fs-agx-delivery">
          <header><Who id={message.author} /><strong>{message.author}</strong>
            <button type="button" className="fs-agx-delivery__title" onClick={() => onOpen({ kind: 'thread', id: String(message.thread_id) }, message.space_id)}>
              {message.task_id ? '#' + message.task_id + ' ' : ''}{message.thread_title}</button>
            <span className="fs-agx-tag">{name(message.space_id)}</span><time>{stamp(message.created)}</time></header>
          <p className="fs-agx-delivery__meta">{message.body.slice(0, 500)}{message.body.length > 500 ? '…' : ''}</p>
        </article>
      ))}
      {inbox.reviews.length > 0 && <h3 className="fs-agx-feed__title">{t('Submitted, without a reviewer')} <span className="fs-agx-cat__count">{inbox.reviews.length}</span></h3>}
      {inbox.reviews.map((task) => (
        <article key={'r' + task.id} className="fs-agx-delivery" data-status="review">
          <header><Who id={task.owner} />
            <button type="button" className="fs-agx-delivery__title" onClick={() => task.thread_id && onOpen({ kind: 'thread', id: String(task.thread_id) }, task.space_id)}>#{task.id} {task.title}</button>
            <span className="fs-agx-tag">{name(task.space_id)}</span><time>{stamp(task.submitted_at)}</time></header>
        </article>
      ))}
    </div>
  );
}

/* ── Search: where something was said ── */

/** `within`: the slug of a channel the search is kept to (the chip «en: #canal»); `onWithin(null)` widens it again. */
export function SearchView({ guild, servers, query, onQuery, onOpen, within, onWithin }: {
  guild: AgoraGuild; servers: AgoraServer[]; query: string; onQuery: (q: string) => void; onOpen: Open;
  within?: string | null; onWithin?: (slug: string | null) => void;
}) {
  const [text, setText] = useState(query);
  const [everywhere, setEverywhere] = useState(false);
  const [hits, setHits] = useState<SearchHit | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { setText(query); }, [query]);
  useEffect(() => {
    if (query.trim().length < 2) { setHits(null); return; }
    const controller = new AbortController();
    setBusy(true); setError('');
    searchAgora(guild.space.id, query.trim(), everywhere, controller.signal).then(setHits)
      .catch((e: Error) => { if (!controller.signal.aborted) setError(e.message); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [query, everywhere, guild.space.id]);
  const name = (id: string) => servers.find((s) => s.id === id)?.title ?? id;
  const scope = within ? guild.channels.find((c) => c.slug === within) ?? null : null;
  const shown = hits && scope
    ? { ...hits, tasks: [], threads: [], messages: hits.messages.filter((m) => m.thread_id === scope.thread_id && m.space_id === guild.space.id) }
    : hits;
  const total = shown ? shown.tasks.length + shown.threads.length + shown.messages.length : 0;
  return (
    <div className="fs-agx-feed" data-testid="agora-search">
      <form className="fs-agx-board__bar" onSubmit={(e) => { e.preventDefault(); onQuery(text.trim()); }}>
        {scope && (
          <span className="fs-agx-chip" data-testid="agora-search-scope">{t('in:')} #{scope.title}
            <button type="button" aria-label={t('Search the whole server')} onClick={() => onWithin?.(null)}><X size={12} aria-hidden="true" /></button></span>
        )}
        <input className="fs-field" autoFocus value={text} onChange={(e) => setText(e.target.value)} placeholder={t('A word, a phrase or a task number')} aria-label={t('Search the Agora')} data-testid="agora-search-input" />
        <Button size="sm" variant="primary" type="submit" icon={Search} label={t('Search')} loading={busy} />
        <label className="fs-switch"><input type="checkbox" checked={everywhere} onChange={(e) => setEverywhere(e.target.checked)} /> {t('In every server')}</label>
      </form>
      {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      {shown && !total && <EmptyState icon={Search} title={t('Nothing found')} body={t('No message, task or thread has “{q}”.', { q: shown.q ?? query })} />}
      {shown?.tasks.map((task) => (
        <article key={'t' + task.space_id + task.id} className="fs-agx-delivery" data-status={task.status}>
          <header><Who id={task.owner} />
            <button type="button" className="fs-agx-delivery__title" onClick={() => task.thread_id && onOpen({ kind: 'thread', id: String(task.thread_id) }, task.space_id)}>#{task.id} {task.title}</button>
            <span className="fs-agx-status" data-status={task.status}>{t(STATUS_LABEL[task.status] ?? task.status)}</span>
            {everywhere && <span className="fs-agx-tag">{name(task.space_id)}</span>}</header>
        </article>
      ))}
      {shown?.threads.map((thread) => (
        <article key={'h' + thread.id} className="fs-agx-delivery">
          <header><button type="button" className="fs-agx-delivery__title" onClick={() => onOpen({ kind: 'thread', id: String(thread.id) }, thread.space_id)}>{thread.title}</button>
            <span className="fs-agx-tag">{t(THREAD_KIND_LABEL[thread.kind] ?? thread.kind)}</span>
            {everywhere && <span className="fs-agx-tag">{name(thread.space_id)}</span>}</header>
        </article>
      ))}
      {shown?.messages.map((message) => (
        <article key={'m' + message.id} className="fs-agx-activity" style={{ '--agx-c': agentColor(message.author, guildPerson(guild)?.id) } as CSSProperties}>
          <AgentAvatar id={message.author} />
          <div>
            <header><strong>{message.author}</strong>
              <button type="button" className="fs-agx-link" onClick={() => onOpen({ kind: 'thread', id: String(message.thread_id) }, message.space_id)}>
                {message.task_id ? '#' + message.task_id + ' ' : ''}{message.thread_title}</button>
              {everywhere && <span className="fs-agx-tag">{name(message.space_id)}</span>}
              <time>{stamp(message.created)}</time></header>
            <p>{message.snippet}</p>
          </div>
        </article>
      ))}
    </div>
  );
}

/* ── Threads: every open conversation of the server, those that wait for the person first ── */

export function ThreadsView({ guild, onOpen, onNew }: { guild: AgoraGuild; onOpen: Open; onNew: () => void }) {
  const open = guild.threads.filter((thread) => thread.status !== 'escalated');
  return (
    <div className="fs-agx-feed" data-testid="agora-threads">
      <div className="fs-agx-board__bar"><Button size="sm" variant="secondary" icon={Plus} label={t('New thread')} onClick={onNew} /></div>
      {!guild.waiting.length && !open.length && <EmptyState icon={MessagesSquare} title={t('No open threads.')} body={t('A thread is a conversation on one subject: a debate, a question, a handoff.')} />}
      {guild.waiting.map((thread) => (
        <article key={'w' + thread.id} className="fs-agx-delivery" data-status="changes">
          <header><MessageCircleQuestion size={16} aria-hidden="true" />
            <button type="button" className="fs-agx-delivery__title" onClick={() => onOpen({ kind: 'thread', id: String(thread.id) })}>{thread.title}</button>
            <span className="fs-agx-tag" data-kind="escalation">{t('Question for you')}</span><time>{stamp(thread.updated)}</time></header>
          {thread.question && <p className="fs-agx-delivery__meta">{(thread.escalated_by ? thread.escalated_by + ': ' : '') + thread.question}</p>}
        </article>
      ))}
      {open.map((thread) => (
        <article key={'h' + thread.id} className="fs-agx-delivery">
          <header><Who id={thread.last_author || thread.created_by} />
            <button type="button" className="fs-agx-delivery__title" onClick={() => onOpen({ kind: 'thread', id: String(thread.id) })}>{thread.title}</button>
            <span className="fs-agx-tag">{t(THREAD_KIND_LABEL[thread.kind] ?? thread.kind)}</span>
            {thread.mentions > 0 ? <CountBadge n={thread.mentions} mentions /> : thread.unread > 0 ? <CountBadge n={thread.unread} /> : null}
            <time>{stamp(thread.updated)}</time></header>
          <p className="fs-agx-delivery__meta">{tn(thread.messages, '{n} message', '{n} messages')}{thread.last_author ? ' · ' + t('last: {name}', { name: thread.last_author }) : ''}</p>
        </article>
      ))}
    </div>
  );
}

/* ── Welcome: what this server is and what it reads ── */

export function WelcomeView({ guild, onEdit, notify, onRefresh }: { guild: AgoraGuild; onEdit: () => void; notify: Notify; onRefresh: () => void }) {
  const space = guild.space;
  const folders = space.meta.folders ?? [];
  const [busy, setBusy] = useState(false);
  const save = async (next: string[]) => {
    setBusy(true);
    try { await setSpaceMeta(space.id, { folders: next }); onRefresh(); }
    catch (e) { notify((e as Error).message, 'warn'); }
    finally { setBusy(false); }
  };
  const add = async () => {
    try {
      const picked = await pickNative('folder', space.workspace || folders[0] || '');
      if (picked.status === 'ok' && picked.path) await save([...folders, picked.path]);
      else if (picked.status === 'unavailable') onEdit();
    } catch (e) { notify((e as Error).message, 'warn'); }
  };
  const online = guild.members.filter((m) => m.presence !== 'offline' && m.role !== 'person').length;
  return (
    <div className="fs-agx-welcome" data-testid="agora-welcome">
      <h2>{space.title}</h2>
      {space.description && <div className="fs-agx-welcome__about"><Rich text={space.description} /></div>}
      <dl>
        <dt>{t('Members')}</dt><dd>{tn(space.members, '{n} member', '{n} members')} · {tn(online, '{n} around now', '{n} around now')}</dd>
        <dt>{t('Tasks')}</dt><dd>{BOARD_COLUMNS.map((c) => t(COLUMN_LABEL[c]) + ' ' + (guild.column_counts[c] ?? 0)).join(' · ')}</dd>
        <dt>{t('Working folder')}</dt><dd>{space.workspace ? <code>{space.workspace}</code> : <span className="fs-muted">{t('None yet')}</span>}</dd>
        <dt>{t('Context folders')}</dt>
        <dd>
          <ul className="fs-agx-folders">
            {folders.map((folder) => (
              <li key={folder}><FolderOpen size={14} aria-hidden="true" /><code>{folder}</code>
                <IconButton size="sm" icon={Trash2} label={t('Remove {path}', { path: folder })} disabled={busy} onClick={() => void save(folders.filter((f) => f !== folder))} /></li>
            ))}
            {!folders.length && <li className="fs-muted">{t('No extra folders: agents of this server only know its working folder.')}</li>}
          </ul>
          <Button size="sm" variant="secondary" icon={FolderPlus} label={t('Add a context folder')} loading={busy} onClick={() => void add()} testId="agora-add-folder" />
        </dd>
        <dt>{t('For the agents')}</dt>
        <dd className="fs-agx-welcome__cli">
          <code>agora --as &lt;id&gt; --space {space.id} say general "…"</code>
          <code>agora --as &lt;id&gt; --space {space.id} read general</code>
          <code>agora --as &lt;id&gt; --space {space.id} board</code>
        </dd>
      </dl>
      <Button size="sm" variant="ghost" label={t('Edit server…')} onClick={onEdit} />
    </div>
  );
}
